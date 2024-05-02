import json
import re
import uuid
from typing import Optional, cast

from core.app.entities.app_invoke_entities import ModelConfigWithCredentialsEntity
from core.memory.token_buffer_memory import TokenBufferMemory
from core.model_manager import ModelInstance
from core.model_runtime.entities.llm_entities import LLMResult, LLMUsage
from core.model_runtime.entities.message_entities import (
    AssistantPromptMessage,
    PromptMessage,
    PromptMessageRole,
    PromptMessageTool,
    ToolPromptMessage,
    UserPromptMessage,
)
from core.model_runtime.entities.model_entities import ModelFeature, ModelPropertyKey
from core.model_runtime.model_providers.__base.large_language_model import LargeLanguageModel
from core.model_runtime.utils.encoders import jsonable_encoder
from core.prompt.advanced_prompt_transform import AdvancedPromptTransform
from core.prompt.entities.advanced_prompt_entities import ChatModelMessage, CompletionModelPromptTemplate
from core.prompt.simple_prompt_transform import ModelMode
from core.prompt.utils.prompt_message_util import PromptMessageUtil
from core.workflow.entities.node_entities import NodeRunMetadataKey, NodeRunResult, NodeType
from core.workflow.entities.variable_pool import VariablePool
from core.workflow.nodes.llm.entities import ModelConfig
from core.workflow.nodes.llm.llm_node import LLMNode
from core.workflow.nodes.parameter_extractor.entities import ParameterExtractorNodeData
from core.workflow.nodes.parameter_extractor.prompts import (
    CHAT_EXAMPLE,
    CHAT_GENERATE_JSON_USER_MESSAGE_TEMPLATE,
    COMPLETION_GENERATE_JSON_PROMPT,
    FUNCTION_CALLING_EXTRACTOR_EXAMPLE,
    FUNCTION_CALLING_EXTRACTOR_NAME,
    FUNCTION_CALLING_EXTRACTOR_SYSTEM_PROMPT,
)
from extensions.ext_database import db
from models.workflow import WorkflowNodeExecutionStatus


class ParameterExtractorNode(LLMNode):
    """
    Parameter Extractor Node.
    """
    _node_data_cls = ParameterExtractorNodeData
    _node_type = NodeType.PARAMETER_EXTRACTOR

    def _run(self, variable_pool: VariablePool) -> NodeRunResult:
        """
        Run the node.
        """

        node_data = cast(ParameterExtractorNodeData, self.node_data)
        query = variable_pool.get_variable_value(node_data.query)
        if not query:
            raise ValueError("Query not found")
        
        inputs={
            'query': query,
            'parameters': node_data.parameters,
            'instruction': node_data.instruction,
        }
        
        model_instance, model_config = self._fetch_model_config(node_data.model)
        if not isinstance(model_instance.model_type_instance, LargeLanguageModel):
            raise ValueError("Model is not a Large Language Model")
        
        llm_model = model_instance.model_type_instance
        model_schema = llm_model.get_model_schema(model_config.model, model_config.credentials)
        if not model_schema:
            raise ValueError("Model schema not found")
        
        # fetch memory
        memory = self._fetch_memory(node_data.memory, variable_pool, model_instance)
        
        if set(model_schema.features or []) & set([ModelFeature.MULTI_TOOL_CALL, ModelFeature.MULTI_TOOL_CALL]):
            # use function call 
            prompt_messages, prompt_message_tools = self._generate_function_call_prompt(
                node_data, query, model_config, memory
            )
        else:
            # use prompt engineering
            prompt_messages = self._generate_prompt_engineering_prompt(node_data, query)
            prompt_message_tools = []

        process_data = {
            'model_mode': model_config.mode,
            'prompts': PromptMessageUtil.prompt_messages_to_prompt_for_saving(
                model_mode=model_config.mode,
                prompt_messages=prompt_messages
            ),
            'usage': None,
            'function': {} if not prompt_message_tools else jsonable_encoder(prompt_message_tools[0]),
            'tool_call': None,
        }

        try:
            text, usage, tool_call = self._invoke_llm(
                node_data_model=node_data.model,
                model_instance=model_instance,
                prompt_messages=prompt_messages,
                tools=prompt_message_tools,
                stop=model_config.stop,
            )
            process_data['usage'] = jsonable_encoder(usage)
            process_data['tool_call'] = jsonable_encoder(tool_call)
        except Exception as e:
            return NodeRunResult(
                status=WorkflowNodeExecutionStatus.FAILED,
                inputs=inputs,
                process_data={},
                outputs={'__error__': str(e)},
                error=str(e),
                metadata={}
            )

        error = ''

        if tool_call:
            result = self._extract_json_from_tool_call(tool_call)
        else:
            result = self._extract_complete_json_response(text)
            if not result:
                result = self._generate_default_result(node_data)
                error = "Failed to extract result from function call or text response, using empty result."

        try:
            result = self._validate_result(node_data, result)
        except Exception as e:
            error = str(e)

        # transform result into standard format
        result = self._transform_result(node_data, result)

        return NodeRunResult(
            status=WorkflowNodeExecutionStatus.SUCCEEDED,
            inputs=inputs,
            process_data=process_data,
            outputs={
                '__error__': error,
                **result,
            },
            metadata={
                NodeRunMetadataKey.TOTAL_TOKENS: usage.total_tokens,
                NodeRunMetadataKey.TOTAL_PRICE: usage.total_price,
                NodeRunMetadataKey.CURRENCY: usage.currency
            }
        )

    def _invoke_llm(self, node_data_model: ModelConfig,
                    model_instance: ModelInstance,
                    prompt_messages: list[PromptMessage],
                    tools: list[PromptMessageTool],
                    stop: list[str]) -> tuple[str, LLMUsage, Optional[AssistantPromptMessage.ToolCall]]:
        """
        Invoke large language model
        :param node_data_model: node data model
        :param model_instance: model instance
        :param prompt_messages: prompt messages
        :param stop: stop
        :return:
        """
        db.session.close()

        invoke_result = model_instance.invoke_llm(
            prompt_messages=prompt_messages,
            model_parameters=node_data_model.completion_params,
            tools=tools,
            stop=stop,
            stream=False,
            user=self.user_id,
        )

        # handle invoke result
        if not isinstance(invoke_result, LLMResult):
            raise ValueError(f"Invalid invoke result: {invoke_result}")
        
        text = invoke_result.message.content
        usage = invoke_result.usage
        tool_call = invoke_result.message.tool_calls[0] if invoke_result.message.tool_calls else None

        # deduct quota
        self.deduct_llm_quota(tenant_id=self.tenant_id, model_instance=model_instance, usage=usage)

        return text, usage, tool_call

    def _generate_function_call_prompt(self, 
        node_data: ParameterExtractorNodeData,
        query: str,
        model_config: ModelConfigWithCredentialsEntity,
        memory: Optional[TokenBufferMemory],
    ) -> tuple[list[PromptMessage], list[PromptMessageTool]]:
        """
        Generate function call prompt.
        """
        prompt_transform = AdvancedPromptTransform(with_variable_tmpl=True)
        rest_token = self._calculate_rest_token(node_data, query, model_config, '')
        prompt_template = self._get_function_calling_prompt_template(node_data, query, memory, rest_token)
        prompt_messages = prompt_transform.get_prompt(
            prompt_template=prompt_template,
            inputs={},
            query='',
            files=[],
            context='',
            memory_config=node_data.memory,
            memory=None,
            model_config=model_config
        )

        # find last user message
        last_user_message_idx = -1
        for i, prompt_message in enumerate(prompt_messages):
            if prompt_message.role == PromptMessageRole.USER:
                last_user_message_idx = i

        # add function call messages before last user message
        example_messages = []
        for example in FUNCTION_CALLING_EXTRACTOR_EXAMPLE:
            id = uuid.uuid4().hex
            example_messages.extend([
                UserPromptMessage(content=example['user']['query']),
                AssistantPromptMessage(
                    content=example['assistant']['text'],
                    tool_calls=[
                        AssistantPromptMessage.ToolCall(
                            id=id,
                            type='function',
                            function=AssistantPromptMessage.ToolCall.ToolCallFunction(
                                name=example['assistant']['function_call']['name'],
                                arguments=json.dumps(example['assistant']['function_call']['parameters']
                            )
                        ))
                    ]
                ),
                ToolPromptMessage(
                    content='Great! You have called the function with the correct parameters.',
                    tool_call_id=id
                ),
                AssistantPromptMessage(
                    content='I have extracted the parameters, let\'s move on.',
                )
            ])

        prompt_messages = prompt_messages[:last_user_message_idx] + \
            example_messages + prompt_messages[last_user_message_idx:]
        
        # generate tool
        tool = PromptMessageTool(
            name=FUNCTION_CALLING_EXTRACTOR_NAME,
            description='Extract parameters from the natural language text',
            parameters=node_data.get_parameter_json_schema(),
        )

        return prompt_messages, [tool]

    def _generate_prompt_engineering_prompt(self, 
        data: ParameterExtractorNodeData,
        query: str,
        model_config: ModelConfigWithCredentialsEntity,
        memory: Optional[TokenBufferMemory],
    ) -> list[PromptMessage]:
        """
        Generate prompt engineering prompt.
        """
        model_mode = ModelMode.value_of(data.model.mode)

        if model_mode == ModelMode.COMPLETION:
            return self._generate_prompt_engineering_completion_prompt(
                data, query, model_config, memory
            )
        elif model_mode == ModelMode.CHAT:
            return self._generate_prompt_engineering_chat_prompt(
                data, query, model_config, memory
            )
        else:
            raise ValueError(f"Invalid model mode: {model_mode}")

    def _generate_prompt_engineering_completion_prompt(self,
        node_data: ParameterExtractorNodeData,
        query: str,
        model_config: ModelConfigWithCredentialsEntity,
        memory: Optional[TokenBufferMemory],
    ) -> list[PromptMessage]:
        """
        Generate completion prompt.
        """
        prompt_transform = AdvancedPromptTransform(with_variable_tmpl=True)
        rest_token = self._calculate_rest_token(node_data, query, model_config, '')
        prompt_template = self._get_prompt_engineering_prompt_template(node_data, query, memory, rest_token)
        prompt_messages = prompt_transform.get_prompt(
            prompt_template=prompt_template,
            inputs={
                'structure': json.dumps(node_data.get_parameter_json_schema())
            },
            query='',
            files=[],
            context='',
            memory_config=node_data.memory,
            memory=memory,
            model_config=model_config
        )

        return prompt_messages

    def _generate_prompt_engineering_chat_prompt(self,
        node_data: ParameterExtractorNodeData,
        query: str,
        model_config: ModelConfigWithCredentialsEntity,
        memory: Optional[TokenBufferMemory],
    ) -> list[PromptMessage]:
        """
        Generate chat prompt.
        """
        prompt_transform = AdvancedPromptTransform(with_variable_tmpl=True)
        rest_token = self._calculate_rest_token(node_data, query, model_config, '')
        prompt_template = self._get_prompt_engineering_prompt_template(node_data, query, memory, rest_token)
        prompt_messages = prompt_transform.get_prompt(
            prompt_template=prompt_template,
            inputs={},
            query=CHAT_GENERATE_JSON_USER_MESSAGE_TEMPLATE.format(
                structure=json.dumps(node_data.get_parameter_json_schema()),
                text=query,
            ),
            files=[],
            context='',
            memory_config=node_data.memory,
            memory=memory,
            model_config=model_config
        )

        # find last user message
        last_user_message_idx = -1
        for i, prompt_message in enumerate(prompt_messages):
            if prompt_message.role == PromptMessageRole.USER:
                last_user_message_idx = i

        # add example messages before last user message
        example_messages = []
        for example in CHAT_EXAMPLE:
            example_messages.extend([
                UserPromptMessage(content=CHAT_GENERATE_JSON_USER_MESSAGE_TEMPLATE.format(
                    structure=json.dumps(example['user']['json']),
                    text=example['user']['query'],
                )),
                AssistantPromptMessage(
                    content=json.dumps(example['assistant']['json']),
                )
            ])

        prompt_messages = prompt_messages[:last_user_message_idx] + \
            example_messages + prompt_messages[last_user_message_idx:]

        return prompt_messages

    def _validate_result(self, data: ParameterExtractorNodeData, result: dict) -> dict:
        """
        Validate result.
        """
        if len(data.parameters) != len(result):
            raise ValueError("Invalid number of parameters")
        
        for parameter in data.parameters:
            if parameter.required and parameter.name not in result:
                raise ValueError(f"Parameter {parameter.name} is required")
            
            if parameter.type == 'select' and parameter.options and result.get(parameter.name) not in parameter.options:
                raise ValueError(f"Invalid value for parameter {parameter.name}")
            
            if parameter.type == 'number' and not isinstance(result.get(parameter.name), int | float):
                raise ValueError(f"Invalid value for parameter {parameter.name}")
            
            if parameter.type == 'bool' and not isinstance(result.get(parameter.name), bool):
                raise ValueError(f"Invalid value for parameter {parameter.name}")
            
            if parameter.type == 'string' and not isinstance(result.get(parameter.name), str):
                raise ValueError(f"Invalid value for parameter {parameter.name}")
            
        return result

    def _transform_result(self, data: ParameterExtractorNodeData, result: dict) -> dict:
        """
        Transform result into standard format.
        """
        transformed_result = {}
        for parameter in data.parameters:
            if parameter.name in result:
                # transform value
                if parameter.type == 'number':
                    if isinstance(result[parameter.name], int | float):
                        transformed_result[parameter.name] = result[parameter.name]
                    elif isinstance(result[parameter.name], str):
                        try:
                            if '.' in result[parameter.name]:
                                result[parameter.name] = float(result[parameter.name])
                            else:
                                result[parameter.name] = int(result[parameter.name])
                        except ValueError:
                            pass
                    else:
                        pass
                elif parameter.type == 'bool':
                    if isinstance(result[parameter.name], bool):
                        transformed_result[parameter.name] = result[parameter.name]
                    elif isinstance(result[parameter.name], str):
                        if result[parameter.name].lower() in ['true', 'false']:
                            transformed_result[parameter.name] = result[parameter.name].lower() == 'true'
                    elif isinstance(result[parameter.name], int):
                        transformed_result[parameter.name] = bool(result[parameter.name])
                elif parameter.type in ['string', 'select']:
                    if isinstance(result[parameter.name], str):
                        transformed_result[parameter.name] = result[parameter.name]

            if parameter.name not in transformed_result:
                if parameter.type == 'number':
                    transformed_result[parameter.name] = 0
                elif parameter.type == 'bool':
                    transformed_result[parameter.name] = False
                elif parameter.type in ['string', 'select']:
                    transformed_result[parameter.name] = ''

        return transformed_result

    def _extract_complete_json_response(self, result: str) -> Optional[dict]:
        """
        Extract complete json response.
        """
        def find_json_end(text, start):
            stack = []
            for i, char in enumerate(text[start:]):
                if char in ['{', '[']:
                    stack.append(char)
                elif char in ['}', ']']:
                    if stack and ((char == '}' and stack[-1] == '{') or (char == ']' and stack[-1] == '[')):
                        stack.pop()
                        if not stack:
                            return start + i
            return -1
        
        json_patterns = r'\b(?:json\s*[:=]|{\s*["\w])'
        matches = re.finditer(json_patterns, result, re.DOTALL | re.IGNORECASE)
        
        for match in matches:
            start = match.start()
            end = find_json_end(result, start)
            if end != -1:
                try:
                    json_str = result[start:end+1]
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    continue
        
        return None

    def _extract_json_from_tool_call(self, tool_call: AssistantPromptMessage.ToolCall) -> Optional[dict]:
        """
        Extract json from tool call.
        """
        if not tool_call or not tool_call.function.arguments:
            return None
        
        return json.loads(tool_call.function.arguments)
    
    def _generate_default_result(self, data: ParameterExtractorNodeData) -> dict:
        """
        Generate default result.
        """
        result = {}
        for parameter in data.parameters:
            if parameter.type == 'number':
                result[parameter.name] = 0
            elif parameter.type == 'bool':
                result[parameter.name] = False
            elif parameter.type in ['string', 'select']:
                result[parameter.name] = ''
        
        return result

    def _get_function_calling_prompt_template(self, node_data: ParameterExtractorNodeData, query: str,
                             memory: Optional[TokenBufferMemory],
                             max_token_limit: int = 2000) \
            -> list[ChatModelMessage]:
        model_mode = ModelMode.value_of(node_data.model.mode)
        input_text = query
        memory_str = ''
        instruction = node_data.instruction or ''
        if memory:
            memory_str = memory.get_history_prompt_text(max_token_limit=max_token_limit,
                                                        message_limit=node_data.memory.window.size)
        if model_mode == ModelMode.CHAT:
            system_prompt_messages = ChatModelMessage(
                role=PromptMessageRole.SYSTEM,
                text=FUNCTION_CALLING_EXTRACTOR_SYSTEM_PROMPT.format(histories=memory_str, instruction=instruction)
            )
            user_prompt_message = ChatModelMessage(
                role=PromptMessageRole.USER,
                text=input_text
            )
            return [system_prompt_messages, user_prompt_message]
        else:
            raise ValueError(f"Model mode {model_mode} not support.")
        
    def _get_prompt_engineering_prompt_template(self, node_data: ParameterExtractorNodeData, query: str,
                                                memory: Optional[TokenBufferMemory],
                                                max_token_limit: int = 2000) \
        -> list[ChatModelMessage]:

        model_mode = ModelMode.value_of(node_data.model.mode)
        input_text = query
        memory_str = ''
        instruction = node_data.instruction or ''
        if memory:
            memory_str = memory.get_history_prompt_text(max_token_limit=max_token_limit,
                                                        message_limit=node_data.memory.window.size)
        if model_mode == ModelMode.CHAT:
            system_prompt_messages = ChatModelMessage(
                role=PromptMessageRole.SYSTEM,
                text=FUNCTION_CALLING_EXTRACTOR_SYSTEM_PROMPT.format(histories=memory_str, instruction=instruction)
            )
            user_prompt_message = ChatModelMessage(
                role=PromptMessageRole.USER,
                text=input_text
            )
            return [system_prompt_messages, user_prompt_message]
        elif model_mode == ModelMode.COMPLETION:
            return CompletionModelPromptTemplate(
                text=COMPLETION_GENERATE_JSON_PROMPT.format(histories=memory_str,
                                                            text=input_text,
                                                            instruction=instruction)
            )
        else:
            raise ValueError(f"Model mode {model_mode} not support.")

    def _calculate_rest_token(self, node_data: ParameterExtractorNodeData, query: str,
                              model_config: ModelConfigWithCredentialsEntity,
                              context: Optional[str]) -> int:
        prompt_transform = AdvancedPromptTransform(with_variable_tmpl=True)
        prompt_template = self._get_function_calling_prompt_template(node_data, query, None, 2000)
        prompt_messages = prompt_transform.get_prompt(
            prompt_template=prompt_template,
            inputs={},
            query='',
            files=[],
            context=context,
            memory_config=node_data.memory,
            memory=None,
            model_config=model_config
        )
        rest_tokens = 2000

        model_context_tokens = model_config.model_schema.model_properties.get(ModelPropertyKey.CONTEXT_SIZE)
        if model_context_tokens:
            model_type_instance = model_config.provider_model_bundle.model_type_instance
            model_type_instance = cast(LargeLanguageModel, model_type_instance)

            curr_message_tokens = model_type_instance.get_num_tokens(
                model_config.model,
                model_config.credentials,
                prompt_messages
            ) + 1000 # add 1000 to ensure tool call messages

            max_tokens = 0
            for parameter_rule in model_config.model_schema.parameter_rules:
                if (parameter_rule.name == 'max_tokens'
                        or (parameter_rule.use_template and parameter_rule.use_template == 'max_tokens')):
                    max_tokens = (model_config.parameters.get(parameter_rule.name)
                                  or model_config.parameters.get(parameter_rule.use_template)) or 0

            rest_tokens = model_context_tokens - max_tokens - curr_message_tokens
            rest_tokens = max(rest_tokens, 0)

        return rest_tokens