"""Build a bounded, whole-library planning inventory from validated distillation."""
import argparse
import json
from pathlib import Path
from collections import Counter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--distill-dir', type=Path)
    args = parser.parse_args()
    root = args.run_dir
    distilled_root = args.distill_dir or root / 'distill'
    source = json.loads((root / 'resources.json').read_text(encoding='utf-8'))['resources']
    distilled = json.loads((distilled_root / 'combined.json').read_text(encoding='utf-8'))
    items = {item['resource_id']: item for item in distilled['items']}
    if len(items) != len(distilled['items']) or set(items) != {r['id'] for r in source}:
        raise ValueError('Planning requires validated distillation covering every resource exactly once')
    reference = {
        'name': 'reference-guided-induction',
        'organizing_principle': '主题与长期使用任务为主入口，形式/产品/交叉主题使用标签；从全库统一归纳',
        'preserve_roots': [],
        'observed_structure': ['开发 & 编程', 'Crypto & 金融', '学习 & 资料', '工具 & 在线服务', '娱乐 / 视频 / 游戏', '社交 & 社区', '生活 / 购物 / 出行'],
        'obsidian_observations': ['学习与开发', '素材与参考', '发布与运营', '工具与方法'],
        'expected_draft_reported_before_api': ['开发与编程（含游戏开发）', '美术与创作', 'Crypto与金融', '学习与资料', '工具与在线服务', '新闻与公共事务', '娱乐', '社交', '生活'],
        'naming_constraints': '简明中文与必要技术专名；目录名用 & 或 与，不用 /，避免将显示分隔符误认层级',
        'user_constraints': ['单一主目录；主题与用途优先于平台、载体', '相近网站跨批次一致归属，细分类不按单网站品牌创建', '以上入口是参考偏好和已汇报预期，不是数量要求；解释实际调整'],
    }
    # Every resource remains represented. Sizes below are planning-only excerpts,
    # never replacements for literal evidence during classification.
    topics = []
    for item in distilled['candidate_topics']:
        topics.append([item['label'], item['description'][:64], len(item['support_ids']), item['support_ids'][:2]])
    payload = {
        'taxonomy_id': 'bookmarks-reference-20261010', 'next_version': 1,
        'reference_profile': reference, 'previous_taxonomy': None,
        'resource_inventory': {'total': len(source), 'columns': ['id', 'summary', 'primary_use', 'evidence_state'],
                               'rows': [], 'candidate_topic_columns': ['label', 'description', 'support_count_in_source_batch', 'sample_ids'],
                               'candidate_topics': topics,
                               'evidence_state_codes': {'S': 'sufficient', 'I': 'insufficient', 'C': 'conflicting'},
                               'evidence_counts': dict(Counter(x['evidence_state'] for x in items.values()))},
    }
    chosen = None
    for summary_limit, purpose_limit, topic_limit, samples in [(80, 30, 64, 2), (60, 22, 40, 1), (45, 16, 24, 1), (32, 12, 16, 1)]:
        payload['resource_inventory']['rows'] = [[r['id'], items[r['id']]['summary'][:summary_limit],
                                                items[r['id']]['primary_use'][:purpose_limit], items[r['id']]['evidence_state'][0].upper()]
                                               for r in source]
        payload['resource_inventory']['candidate_topics'] = [[t[0], t[1][:topic_limit], t[2], t[3][:samples]] for t in topics]
        payload['resource_inventory']['truncation_limits'] = {'summary': summary_limit, 'primary_use': purpose_limit, 'candidate_description': topic_limit, 'sample_ids_per_topic': samples}
        size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
        if size <= 52500:
            chosen = size
            break
    if chosen is None:
        raise ValueError('Inventory is too large; do not silently drop resources or topics')
    (root / 'plan-input.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'resources': len(source), 'candidate_topics': len(topics), 'input_characters': chosen,
                      'truncation_limits': payload['resource_inventory']['truncation_limits']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
