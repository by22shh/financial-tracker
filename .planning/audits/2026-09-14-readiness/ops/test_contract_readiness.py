from __future__ import annotations
import json
import pytest
from fintracker.infra.ai.schemas import json_schema_for, ExtractionResponse, ReceiptResponse, RecommendationResponse, AnalyticsPlan

@pytest.mark.parametrize('model',[ExtractionResponse, ReceiptResponse, RecommendationResponse, AnalyticsPlan])
def test_provider_schema_satisfies_documented_required_contract(model):
    violations=[]
    def walk(value,path='$'):
        if isinstance(value,dict):
            if value.get('type')=='object':
                missing=set(value.get('properties',{}))-set(value.get('required',[]))
                if missing:violations.append((path,sorted(missing)))
            for key,item in value.items():walk(item,path+'.'+key)
        elif isinstance(value,list):
            for i,item in enumerate(value):walk(item,path+f'[{i}]')
    walk(json_schema_for(model))
    assert not violations, f'OpenAI strict contract: all properties required, missing {violations}'

async def test_image_bytes_validated_before_paid_provider(owner_session,test_settings,monkeypatch):
    from fintracker.application.conversation.media import handle_media
    from fintracker.application.conversation.types import Attachment, IncomingMessage, MessageKind
    from fintracker.application.intelligence import media_pipeline
    from fintracker.infra.ai.openai_client import ScriptedAIProvider,set_provider_override
    from tests.integration.test_deep_audit import prepared
    f=await prepared(owner_session)
    settings=test_settings.model_copy(deep=True);settings.ai.enabled=True
    async def download(settings,*,file_id):return b'This is plain text, not a JPEG, PNG or WebP image.'
    monkeypatch.setattr(media_pipeline,'download_attachment',download)
    provider=ScriptedAIProvider(responses=[json.dumps({'schema_version':'1.0','document_kind':'receipt','payment_confirmed':True,'total_decimal':'450.00','currency':'RUB','lines':[],'unreadable_lines':0})])
    set_provider_override(provider)
    try:
        replies=await handle_media(settings,IncomingMessage(telegram_user_id=f.user.telegram_user_id,chat_id=f.user.telegram_user_id,workspace_id=f.workspace.id,kind=MessageKind.DOCUMENT,message_id=555,
          attachments=(Attachment(file_id='corrupt',kind='document',mime_type='image/jpeg',size_bytes=50,file_name='receipt.jpg'),)),user_id=f.user.id)
        assert not provider.calls, f'Corrupt text bytes reached vision provider; replies={replies}'
    finally:set_provider_override(None)
