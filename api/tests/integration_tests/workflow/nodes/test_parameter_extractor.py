import os
from unittest.mock import MagicMock

import pytest

from core.app.entities.app_invoke_entities import ModelConfigWithCredentialsEntity
from core.entities.provider_configuration import ProviderConfiguration, ProviderModelBundle
from core.entities.provider_entities import CustomConfiguration, CustomProviderConfiguration, SystemConfiguration
from core.model_manager import ModelInstance
from core.model_runtime.entities.model_entities import ModelType
from core.model_runtime.model_providers.model_provider_factory import ModelProviderFactory
from core.workflow.entities.node_entities import SystemVariable
from core.workflow.entities.variable_pool import VariablePool
from core.workflow.nodes.base_node import UserFrom
from core.workflow.nodes.parameter_extractor.parameter_extractor_node import ParameterExtractorNode
from extensions.ext_database import db
from models.provider import ProviderType

"""FOR MOCK FIXTURES, DO NOT REMOVE"""
from models.workflow import WorkflowNodeExecutionStatus
from tests.integration_tests.model_runtime.__mock.openai import setup_openai_mock


def get_mocked_fetch_model_config():
    credentials = {
        'openai_api_key': os.environ.get('OPENAI_API_KEY')
    }

    provider_instance = ModelProviderFactory().get_provider_instance('openai')
    model_type_instance = provider_instance.get_model_instance(ModelType.LLM)
    provider_model_bundle = ProviderModelBundle(
        configuration=ProviderConfiguration(
            tenant_id='1',
            provider=provider_instance.get_provider_schema(),
            preferred_provider_type=ProviderType.CUSTOM,
            using_provider_type=ProviderType.CUSTOM,
            system_configuration=SystemConfiguration(
                enabled=False
            ),
            custom_configuration=CustomConfiguration(
                provider=CustomProviderConfiguration(
                    credentials=credentials
                )
            )
        ),
        provider_instance=provider_instance,
        model_type_instance=model_type_instance
    )
    model_instance = ModelInstance(provider_model_bundle=provider_model_bundle, model='gpt-3.5-turbo')
    model_config = ModelConfigWithCredentialsEntity(
        model='gpt-3.5-turbo',
        provider='openai',
        mode='chat',
        credentials=credentials,
        parameters={},
        model_schema=model_type_instance.get_model_schema('gpt-3.5-turbo'),
        provider_model_bundle=provider_model_bundle
    )

    return MagicMock(return_value=tuple([model_instance, model_config]))

@pytest.mark.parametrize('setup_openai_mock', [['chat']], indirect=True)
def test_function_calling_parameter_extractor(setup_openai_mock):
    """
    Test function calling for parameter extractor.
    """
    node = ParameterExtractorNode(
        tenant_id='1',
        app_id='1',
        workflow_id='1',
        user_id='1',
        user_from=UserFrom.ACCOUNT,
        config={
            'id': 'llm',
            'data': {
                'title': '123',
                'type': 'parameter-extractor',
                'model': {
                    'provider': 'openai',
                    'name': 'gpt-3.5-turbo',
                    'mode': 'chat',
                    'completion_params': {}
                },
                'query': ['sys', 'query'],
                'parameters': [{
                    'name': 'location',
                    'type': 'string',
                    'description': 'location',
                    'required': True
                }],
                'instruction': '',
                'memory': None,
            }
        }
    )

    node._fetch_model_config = get_mocked_fetch_model_config()
    db.session.close = MagicMock()

    # construct variable pool
    pool = VariablePool(system_variables={
        SystemVariable.QUERY: 'what\'s the weather in SF',
        SystemVariable.FILES: [],
        SystemVariable.CONVERSATION_ID: 'abababa',
        SystemVariable.USER_ID: 'aaa'
    }, user_inputs={})

    result = node.run(pool)

    assert result.status == WorkflowNodeExecutionStatus.SUCCEEDED
    assert result.outputs.get('location') == 'kawaii'
    assert result.outputs.get('__error__') == ''