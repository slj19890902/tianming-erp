import json
from io import BytesIO
import pytest
from app.services.ai.providers import DeepSeekInventoryInsightProvider, ProviderLimits, ProviderUnavailable, inventory_provider_status, resolve_inventory_provider
from tests.test_ai_openai_provider import _snapshot, _interpretation

def test_deepseek_request_output_usage_and_separate_credentials(monkeypatch):
    monkeypatch.setenv('ERP_AI_INVENTORY_PROVIDER','deepseek')
    monkeypatch.delenv('ERP_AI_INVENTORY_MODEL',raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    monkeypatch.setenv('OPENAI_API_KEY','sk-openai-must-not-be-used')
    assert not inventory_provider_status()['enabled']
    monkeypatch.setenv('DEEPSEEK_API_KEY','sk-deepseek-test-secret-123456')
    provider=resolve_inventory_provider()
    assert provider.model_code=='deepseek-flash'
    def opener(request,timeout):
        assert request.full_url=='https://api.deepseek.com/chat/completions'
        assert request.headers['Authorization']=='Bearer sk-deepseek-test-secret-123456'
        body=json.loads(request.data)
        assert body['response_format']=={'type':'json_object'}
        assert 'json' in body['messages'][0]['content'].lower()
        assert 'tools' not in body and 'store' not in body
        return BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(_interpretation())}}],'usage':{'prompt_tokens':11,'completion_tokens':22}}).encode())
    provider._opener=opener
    result=provider.generate(_snapshot(),prompt_version='test',limits=ProviderLimits())
    assert result.provider_code=='deepseek' and result.output==_interpretation()
    assert (result.input_tokens,result.output_tokens)==(11,22)

@pytest.mark.parametrize('content,finish',[('', 'stop'),('{}','length'),('[]','stop')])
def test_deepseek_rejects_empty_truncated_or_invalid_output(content,finish):
    p=DeepSeekInventoryInsightProvider('sk-deepseek-test-secret-123456',model_code='deepseek-flash',opener=lambda *a,**kw:BytesIO(json.dumps({'choices':[{'finish_reason':finish,'message':{'content':content}}]}).encode()))
    with pytest.raises(ProviderUnavailable):
        p.generate(_snapshot(),prompt_version='test',limits=ProviderLimits())
