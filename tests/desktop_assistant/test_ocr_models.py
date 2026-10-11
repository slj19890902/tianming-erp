import hashlib
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
import sys
from desktop_assistant.ocr_models import copy_models, prepare_readonly_layout
from desktop_assistant.build import runtime_copy_ignore


class OcrModelTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' and os.getuid() != 0, 'unprivileged native Mac layout')
    def test_native_reader_initializes_in_readonly_prebuilt_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            model = root/'model'
            model.mkdir()
            prepare_readonly_layout(model)
            marker = root/'user_network/.keep'
            marker.chmod(0o444)
            for path in (model, marker.parent, root): path.chmod(0o555)
            try:
                script = '''
import sys
from pathlib import Path
import easyocr
root = Path(sys.argv[1])
try:
    easyocr.Reader(['ch_sim', 'en'], gpu=False, detector=False, recognizer=False,
        download_enabled=False, verbose=False, model_storage_directory=str(root/'model'),
        user_network_directory=str(root/'missing'))
except PermissionError:
    pass
else:
    raise AssertionError('readonly parent unexpectedly allowed a new directory')
reader = easyocr.Reader(['ch_sim', 'en'], gpu=False, detector=False, recognizer=False,
    download_enabled=False, verbose=False, model_storage_directory=str(root/'model'),
    user_network_directory=str(root/'user_network'))
assert reader.user_network_directory == str(root/'user_network')
assert list((root/'user_network').iterdir()) == [root/'user_network/.keep']
print('readonly_layout_ready')
'''
                result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(root)],
                                        capture_output=True, text=True, timeout=60, check=True)
                self.assertEqual(result.stdout.strip(), 'readonly_layout_ready')
            finally:
                for path in (root, marker.parent, model): path.chmod(0o755)
                marker.chmod(0o644)

    def test_runtime_copy_excludes_packaged_ocr_before_rebuilding_models(self):
        names = ['DLLs', 'Lib', 'Scripts', 'site-packages', '__pycache__', 'ocr']
        self.assertEqual(
            runtime_copy_ignore('unused', names),
            {'Scripts', 'site-packages', '__pycache__', 'ocr'},
        )

    def test_verified_models_and_corrupt_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'source'; source.mkdir()
            site=root/'site'; (site/'easyocr').mkdir(parents=True)
            models={name:{'filename':name,'md5sum':hashlib.md5(name.encode()).hexdigest()}
                    for name in ('craft_mlt_25k.pth','zh_sim_g2.pth')}
            (site/'easyocr/config.py').write_text('models='+repr(models))
            for name in models:(source/name).write_bytes(name.encode())
            self.assertEqual(copy_models(source,site,root/'output'),sorted(models))
            (source/'zh_sim_g2.pth').write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'校验失败'):
                copy_models(source,site,root/'rejected')
            self.assertFalse((root/'rejected').exists())
