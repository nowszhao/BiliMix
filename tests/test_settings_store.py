"""Preferences survive source updates and never become executable Python."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from core import config
from core.settings_store import load_settings,save_settings


def test_web_settings_do_not_modify_source_or_machine_local_config(tmp_path,monkeypatch):
    from core import config_manager
    core=tmp_path/'core';core.mkdir()
    defaults=core/'config.py';defaults.write_text('WATERMARK_TEXT = "source default"\n',encoding='utf-8')
    local=core/'config_local.py';local.write_text('CONFUCIUS4_TTS_ROOT = "custom-model"\n',encoding='utf-8')
    target=tmp_path/'data/settings.json'
    monkeypatch.setattr(config,'BASE_DIR',str(tmp_path));monkeypatch.setattr(config,'SETTINGS_FILE',str(target))
    value='中文 "quoted" \\ path\nsecond line'
    monkeypatch.setattr(config,'WATERMARK_TEXT','before')
    before=defaults.read_bytes(),local.read_bytes()
    updated=config_manager.update_config({'watermark_text':value})
    assert 'WATERMARK_TEXT' in updated
    assert (defaults.read_bytes(),local.read_bytes())==before
    assert load_settings(target)['WATERMARK_TEXT']==value
    if os.name!='nt':assert target.stat().st_mode&0o777==0o600


def test_persisted_settings_load_in_a_fresh_python_process(tmp_path):
    target=tmp_path/'preferences.json';value='自己的水印'
    save_settings(target,{'WATERMARK_TEXT':value})
    env={**os.environ,'BILIMIX_SETTINGS_FILE':str(target),'PYTHONUTF8':'1'}
    code='import json; from core import config; print(json.dumps(config.WATERMARK_TEXT))'
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==value


def test_concurrent_fields_merge_and_unknown_settings_are_rejected(tmp_path):
    target=tmp_path/'settings.json'
    values={'WATERMARK_TEXT':'first','ASS_FONT_SIZE_DEFAULT':36}
    threads=[threading.Thread(target=save_settings,args=(target,{k:v}))for k,v in values.items()]
    for thread in threads:thread.start()
    for thread in threads:thread.join()
    assert load_settings(target)==values
    with pytest.raises(ValueError,match='Unknown persisted setting'):
        save_settings(target,{'WHISPERX_HF_TOKEN':'never-persist-this'})
    assert 'never-persist-this' not in target.read_text(encoding='utf-8')
    assert load_settings(target)==values


def test_invalid_types_or_corrupt_store_do_not_overwrite_existing_data(tmp_path):
    target=tmp_path/'settings.json';save_settings(target,{'ASS_FONT_SIZE_DEFAULT':40})
    before=target.read_bytes()
    with pytest.raises(ValueError):save_settings(target,{'ASS_FONT_SIZE_DEFAULT':'huge'})
    assert target.read_bytes()==before
    target.write_text('{broken',encoding='utf-8')
    with pytest.raises(ValueError):load_settings(target)
