"""Shared ordered work facts, with supplier cutting separate from mold yield."""
from app.services.sheet_cutting_contract import cutting_work_instruction
import re


def production_route_contract(snapshot, *, process=(), printing=False, joining=None,
                              already_cut=False):
    text = ' '.join(str(value or '') for value in process)
    steps = []
    def add(code, label):
        steps.append({'code': code, 'label': label})
    if snapshot and int(snapshot.get('cutting_factor', 1)) > 1 and not already_cut:
        add('sheet_cutting', '分切')
    printable = printing if isinstance(printing, bool) else any(
        str(value or '').strip() not in {'', '无', '无印刷', '无需印刷', '不印刷', '不印'}
        for value in (printing if isinstance(printing, (list, tuple)) else [printing]))
    if printable or '印刷' in re.sub(r'(?:无需|无|不)印刷', '', text):
        add('printing', '印刷')
    # Preserve the recorded relative order of explicit processing facts.
    facts = [('slotting', '开槽'), ('creasing', '压线'), ('die_cutting', '模切'),
             ('corner_cutting', '切角'), ('punching', '冲孔'), ('folding', '折叠'),
             ('laminating', '裱合'), ('binding', '捆扎')]
    present = [(text.index(label), code, label) for code, label in facts if label in text]
    if snapshot and snapshot.get('is_die_cut') and not any(x[1] == 'die_cutting' for x in present):
        present.append((len(text), 'die_cutting', '模切'))
    for _, code, label in sorted(present):
        add(code, label)
    if joining and joining != '无需结合':
        add('joining', joining)
    return {'schema_version': 1, 'steps': steps, 'already_cut': bool(already_cut),
            'sheet_cutting_snapshot': snapshot,
            'cutting_instruction': cutting_work_instruction(snapshot) if snapshot else None}
