"""Prepare a full-coverage, bounded second planning request after directory review."""
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
read = lambda p: json.loads(p.read_text(encoding='utf-8'))
payload = read(root / 'plan-input.json')
old = read(root / 'plan-v1/combined.json')
payload['next_version'] = 2
payload['previous_taxonomy'] = {
    'taxonomy_id': old['taxonomy_id'], 'version': old['version'],
    'categories': [{k: c[k] for k in ('id', 'parent_id', 'name', 'assignable')} for c in old['categories']],
}
payload['previous_taxonomy_note'] = '这里只保留旧草案的ID/父级/名称/可分配性作为概念延续参考；不是完整词表，也不是已批准目录。旧边界不作为事实。'
inventory = payload['resource_inventory']
inventory['candidate_topic_columns'] = ['label', 'support_count_in_source_batch', 'sample_ids']
inventory['candidate_topics'] = [[t[0], t[2], t[3]] for t in inventory['candidate_topics']]
payload['review_feedback'] = [
    '第1版暂不用于分类。9个根只代表已汇报方向，不是硬性数量要求。保留入口简洁与用户可预测性，避免形式词覆盖专业目的。',
    '删除近同义且无分支价值的单子纯容器：business-financial-news/global-business-media、tech-specialized-communities/dev-art-forums、interests-creator-patronage/meetups-creator-sponsorship、family-parenting/parenting-early-childhood。',
    'subscription-payment-services不能作为ai-agents-assistants的父类。AI助手、海外支付是不同用途；底层协议文档/开发者API与使用AI助手之间写清优先规则。',
    'dev-art-forums错误混合开发与美术。ZBrush/Blender交流、艺术家作品集随美术用途；技术讨论随开发；无明确专业重心的社交和泛社区才去社交。',
    'game-industry-ratings中销量/收入/市场调研/用户运营应归游戏开发的发行运营用途，不能混入娱乐试玩/玩家评分。必须覆盖游戏社区运营书和Steam市场数据的查找用途。',
    'digital-lifestyle-wellness将手机规格、硬件搬运、白噪音、地图混为一类，需按真实用途拆开或归回工具/出行/数码。不要用层级掩盖杂糅。',
    '补足实际存在的营销/网站运营/SEO流量分析（如Ahrefs、Similarweb）的目标；整体检查明确主题有合理位置，不能凭目录数量宣称覆盖。',
    '科研论文检索与通用图书平台、数学/统计基础与金融/经济应用要有可执行边界。Web3合约/API开发与投资资讯/链上数据也需明确优先规则。',
    '同义别名只收同义词，例如programming-languages-core的旧alias“游戏开发与引擎架构”过宽且非同义；全部复查。include/exclude应比较真实相邻类别，勿用无关反例。',
    '专业财经资讯与综合媒体新闻按主要领域区分；狭义专业内容不因是新闻就改按形式归属。可以直接向边界明确的父类归属，不要求每条都塞入最细子类。',
]
chars = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
if chars > 53000:
    # Keep every resource and topic; omit only redundant sample IDs in candidates.
    inventory['candidate_topic_columns'] = ['label', 'support_count_in_source_batch']
    inventory['candidate_topics'] = [t[:2] for t in inventory['candidate_topics']]
    chars = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
if chars > 53000:
    raise ValueError(f'Revision payload too large: {chars}')
(root / 'plan-v2-input.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'resources': len(inventory['rows']), 'topics': len(inventory['candidate_topics']), 'characters': chars}))
