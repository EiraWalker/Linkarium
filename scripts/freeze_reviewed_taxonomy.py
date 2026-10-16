"""Record explicit host audit corrections to the API's second taxonomy draft."""
import json
from pathlib import Path
import sys
import resource_classification_run as runner

root = Path(sys.argv[1])
tree = json.loads((root / 'plan-v2-long/combined.json').read_text(encoding='utf-8'))
tree['version'] = 3
nodes = {c['id']: c for c in tree['categories']}
changes = []


def change(key, **fields):
    changes.append({'category_id': key, 'before': {k: nodes[key][k] for k in fields}, 'after': fields})
    nodes[key].update(fields)


change('computer-science-theory', name='算法与人工智能基础',
       description='数据结构、算法复杂度、计算机科学与机器学习/深度学习的原理课程、教材和教学实现',
       include=['算法与数据结构、信息学竞赛知识', '机器学习与深度学习原理课程和教材，含为教学配套的训练代码'],
       exclude=['将既有模型接入具体产品的API、SDK与工程工具', '以金融投资策略为主要目标的机器学习应用'], aliases=['计算机科学理论', '算法基础', '人工智能基础'])
change('programming-languages-core', name='编程语言与应用开发',
       description='通用编程语言规范、语言参考、桌面或Web应用开发与软件设计；专业引擎或AI工程有专门入口时优先专门入口',
       include=['编程语言语法、运行时与内存管理', '桌面GUI、Web应用与通用软件开发实践'],
       exclude=['算法、机器学习原理课程', '游戏引擎专有工程', '模型API集成与AI工程'], aliases=['编程语言参考', '应用开发'])
change('ai-agents-assistants', description='面向普通使用者的通用对话助手、角色互动、提示词与无代码智能体应用',
       include=['通用AI问答与写作助手', '角色对话、提示词模板与不要求开发的智能体应用'],
       exclude=['模型API、SDK、MCP服务器开发与集成目录', '机器学习教材', '专业图像、3D或音频创作', '支付代充'])
change('document-collab-storage', name='文档排版、协作与存储',
       description='文档编辑排版、LaTeX符号公式与技术写作参考、协作白板、简历制作和文件存储分享',
       include=['LaTeX符号、公式排版与使用教程', '文档编辑、白板、简历与翻页书工具', '文件协作与云盘分享'],
       exclude=['论文内容检索与图书阅读平台', '代码与开发资产版本控制'], aliases=['文档协作与存储', '文档排版', 'LaTeX排版'])
change('web-analytics-seo', include=['网站流量、SEO与竞争分析', '面向运营的网站内容变更监测'],
       exclude=['HTTP请求、Webhook接口与邮件集成调试', '游戏行业销量与发行研究'])
change('dev-workflow-git', include=['代码、开发资产版本控制与协作', 'HTTP请求、Webhook和邮件集成测试与调试', '开发环境与工程自动化工具'],
       exclude=['网站流量与SEO营销分析', '普通文件分享及文档排版', '通用聊天助手'])
change('art-creative-ai', description='用于视觉与音频创作的AI生成工具，包括图像、3D、像素动画、语音合成与声音克隆',
       include=nodes['art-creative-ai']['include'] + ['语音合成、声音克隆与音频生成'],
       exclude=['通用对话助手', '语音模型开发API/SDK集成教程', '现成音效素材下载'])
for key in ['game-engine-unreal', 'game-engine-unity']:
    change(key, exclude=nodes[key]['exclude'] + ['以玩法机制、联机同步或商业运营为主要目的时按相应专门类别优先'])
change('game-downloads-consoles', exclude=nodes['game-downloads-consoles']['exclude'] + ['具体Modding、模组制作与游戏开发页面；不因来自玩家Wiki就归娱乐'])

additions = [
    {'id': 'dev-ai-data-engineering', 'parent_id': 'software-dev-systems', 'name': 'AI开发与数据科学',
     'description': '将模型和数据工具用于实际工程、API/SDK集成、OCR开发、MCP服务器与数据科学项目协作',
     'include': ['模型API、SDK、OCR代码包、MCP开发集成与服务器目录', '数据科学工程实践、竞赛数据与项目社区'],
     'exclude': ['机器学习基础原理课与教材配套代码', '无需开发的通用聊天助手', '专门用于量化投资的模型项目'],
     'aliases': ['AI工程', '数据科学开发'], 'assignable': True},
    {'id': 'maps-geospatial-tools', 'parent_id': 'tools-services', 'name': '地图与地理信息',
     'description': '地图制作、地理空间数据检索编辑、GIS栅格和矢量工具及个人地图分享',
     'include': ['卫星地图、DEM与矢量地理数据处理', '自定义地图制作与分享'],
     'exclude': ['游戏世界的程序化地图生成算法', '旅途住宿预订和票务'],
     'aliases': ['GIS工具', '地理信息服务'], 'assignable': True},
]
for category in additions:
    tree['categories'].append(category)
    changes.append({'category_id': category['id'], 'before': None, 'after': category})
tree['rules'] = [r for r in tree['rules'] if not r.startswith('对话式AI助手与开发协议区分') and not r.startswith('通用引擎架构与特定玩法机制区分')]
tree['rules'] += [
    '游戏资源冲突时按明确主任务排序：发行/市场/社区运营 > 联机同步 > 玩法机制/模组制作 > 一般引擎工程；纯图形原理和独立渲染算法归底层图形，引擎专属Shader用法归该引擎。品牌作为标签，不把明确的专门任务拆回品牌目录。',
    'AI原理教材及教学训练代码归算法与人工智能基础；生产API/SDK、OCR包、MCP开发/集成目录、数据科学项目社区归AI开发与数据科学；普通对话助手归在线工具；图像、3D、语音创作产品归AI创意工具。',
    '具体页面用途优先于整站身份：玩家Wiki的Modding页面归开发中的玩法制作，游戏游玩攻略才归娱乐；数学基础课即使在金融课程附近也不因此归金融。',
    'LaTeX使用与公式排版归文档排版工具；论文内容检索归学术检索；地图/GIS工具归地图与地理信息，游戏程序化地图算法归游戏开发。',
    '主题证据不足或没有合适可分配类别时输出needs_review，不能把旧来源文件夹当事实，也不能自建目标目录。',
]
tree['reference_summary'].append('第三版由宿主依据独立目录审计修订第二版API草案；是用户授权的离线评测版本，不代表生产批准。')
validator = runner.load_validator(runner.DEFAULT_SKILL)
validated = validator.taxonomy(tree)
out = root / 'taxonomy-v3.json'
runner.write_json(out, tree)
runner.write_json(root / 'taxonomy-v3-provenance.json', {'base': 'plan-v2-long/combined.json', 'type': 'host_audit_corrections',
    'production_approved': False, 'changes': changes, 'rules': tree['rules'], 'validation': validated,
    'evidence_resource_ids': ['ba9a66cac26d30e4', 'd2c1dadaf0618b9e', '7eed3160873adbbf', '625f4f979c47d1af', '886ebfdaeae2e068', '9dc14996052ef8d1', '3e28c2fef32f57be', '09e899076f3f62e9', '71fb56fd4f63a08b', '6b30d11c052dace8', '21458ae8bab21ee1', '14a20b0aa3f0ed4d']})
resources = json.loads((root / 'resources.json').read_text(encoding='utf-8'))['resources']
prompt = runner.system_prompt(runner.DEFAULT_SKILL, 'classify')
for size in [24, 20, 16, 12, 10]:
    maximum = max(len(runner.canonical({'model': 'gemini-3.8-flash-high', 'messages': [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': runner.canonical({'taxonomy': tree, 'approved_taxonomy_hash': validated['taxonomy_hash'], 'resources': resources[i:i+size], 'existing_tags': [], 'human_corrections': []})}]})) for i in range(0, len(resources), size))
    if maximum < 59500:
        break
else:
    raise ValueError('No safe batch size')
print(json.dumps({'taxonomy': str(out), 'nodes': len(tree['categories']), 'hash': validated['taxonomy_hash'], 'batch_size': size, 'max_payload_characters': maximum}, ensure_ascii=False))
