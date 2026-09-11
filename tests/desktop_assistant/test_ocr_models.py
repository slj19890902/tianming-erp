import hashlib
from pathlib import Path
import tempfile
import unittest
from desktop_assistant.ocr_models import copy_models


class OcrModelTests(unittest.TestCase):
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
