"""Combine independent review and repeat disagreements without changing API assignments."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
flags = defaultdict(list)
sources = []


def read(name):
    path = root / name
    sources.append({'file': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return json.loads(path.read_text(encoding='utf-8'))


labels = {'wrong': '归属可能不正确', 'over_abstain': '可能过度保守', 'uncertain': '现有证据无法确认'}
for file in ['holdout-v4-evaluation.json', 'regression-v4-evaluation.json']:
    for case in read(file)['cases']:
        if case['judgment'] in labels:
            flags[case['resource_id']].append(
                f"独立辅助审核：{labels[case['judgment']]}。{case['evaluation_explanation']} 此判断尚未经用户确认。")

measurement = read('measurements-v4.json')
paths = measurement['validation']['paths']


def label(item):
    return '待人工判断' if item['category_id'] is None else ' / '.join(paths[item['category_id']])


for change in measurement['stability']['changed']:
    flags[change['resource_id']].append(
        f"重复运行分歧：首次为「{label(change['before'])}」，改变顺序后的新请求为「{label(change['after'])}」。当前保留首次建议，请确认检索位置；这不是缓存重复测试。")

for finding in read('audit-v4/combined.json')['findings']:
    for resource_id in finding['resource_ids']:
        flags[resource_id].append(f"API 抽样审计：{finding['evidence']} 建议：{finding['recommendation']} 此建议未自动修改目录或归属。")

rows = read('assignments-v4.json')['items']
by_id = {row['resource_id']: row for row in rows}
if set(flags) - set(by_id):
    raise ValueError('Review flags contain unknown resource IDs')
output = dict(sorted(flags.items()))
(root / 'review-flags.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
summary = {
    'sources': sources, 'assignments_modified': False,
    'flagged_count': len(output),
    'flagged_classified_count': sum(by_id[key]['status'] == 'classified' for key in output),
    'flagged_needs_review_count': sum(by_id[key]['status'] == 'needs_review' for key in output),
    'meaning': 'Additional review annotations; overlapping sets must not be added to needs_review as separate resources.',
}
(root / 'review-flags-provenance.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({key: value for key, value in summary.items() if key != 'sources'}, ensure_ascii=False))
