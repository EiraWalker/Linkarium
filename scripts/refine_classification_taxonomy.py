#!/usr/bin/env python3
"""Produce an auditable v4 taxonomy candidate from observed v3 draft results.

No API calls, production changes, or reassignment of bookmarks are performed.
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import resource_classification_run as runner


def refine(source, resources, assignments):
    candidate = copy.deepcopy(source)
    categories = {item["id"]: item for item in candidate["categories"]}
    changes = []

    def evidence(ids):
        items = []
        for rid in ids:
            resource = resources[rid]
            quote = resource["title"]
            if quote not in resource["title"]:
                raise ValueError("Evidence quote must match its original field")
            previous = assignments.get(rid)
            items.append({
                "resource_id": rid, "field": "title", "quote": quote,
                "previous_assignment": {key: previous.get(key) for key in
                    ("category_id", "status", "review_reason", "reason")} if previous else None,
                "evidence_limit": "Original title documents the affected resource. Previous model assignment is an observation, not a gold label.",
            })
        return items

    def update(cid, reason, ids, **fields):
        before = copy.deepcopy(categories[cid])
        categories[cid].update(fields)
        changes.append({"operation": "update", "category_id": cid,
                        "changed_fields": list(fields), "before": before,
                        "after": copy.deepcopy(categories[cid]), "rationale": reason,
                        "evidence": evidence(ids)})

    update("game-engine-arch", "Android 游戏平台文档与其他 Web/2D 引擎有明确开发用途，但 v3 父类不可分配且仅有 UE/Unity/底层图形子类。开放有限父类收纳，同时排除已有具体子类与专门任务。",
           ["440c90896ecca986", "4966e788dd694f3b"],
           name="游戏引擎与平台开发", assignable=True,
           description="游戏引擎与平台开发总类；直接归入仅限跨引擎开发、Android 游戏平台 SDK 或未设专门子类的 2D/Web 引擎。",
           include=["跨引擎的游戏开发工具与平台 SDK", "Android 原生游戏开发及未设专门子类的 2D/Web 游戏引擎"],
           exclude=["Unity、虚幻引擎专属工程或底层图形算法：归相应子类", "明确玩法、联机、游戏项目运营任务：归对应专类；一般 Android 应用归应用开发"],
           aliases=["游戏引擎与平台技术", "跨引擎游戏开发"])

    update("game-mechanics-design", "避免名称只强调设计理论而漏掉技能、背包、交互叙事等明确玩法子系统实现；引擎仅为实现手段时不覆盖具体用途。",
           ["209f0b649eef8d00", "d2061f9bebcd11d7", "3fbcf5d29df5724a"],
           name="玩法设计与实现",
           description="游戏玩法、关卡、数值、交互叙事及明确游戏子系统的设计与代码实现。",
           include=["玩法机制、关卡、数值平衡与交互叙事设计", "技能、背包、战斗、任务、存档等明确游戏子系统实现"],
           exclude=["多人联网与状态同步：归游戏联机", "引擎通用 API、编辑器与构建工程：归对应引擎；制作推进与营销归游戏项目运营"],
           aliases=["游戏玩法与子系统", "游戏机制与数值设计"])

    update("game-publishing-operations", "独立制作的计划、进度与完工经验属于游戏项目工作流；普通 App 宣传工具缺少游戏限定，不得凭开发者身份归入游戏。",
           ["0d964a4d8448ce0a", "9bdb42a8333f2940", "d72f901a1384732a"],
           name="游戏项目与发行运营",
           description="明确面向游戏的项目计划与制作推进、独立制作管理、行业市场研究、发行营销与玩家社区运营。",
           include=["游戏制作计划、进度、团队协作与完工经验", "游戏行业数据、发行营销、游戏商店优化与玩家社区运营"],
           exclude=["游戏内部玩法与程序实现：归玩法或引擎类别", "无游戏限定的网站/App 运营、ASO 与截图宣传工具：归网站与应用运营；玩家购买游玩归娱乐"],
           aliases=["游戏制作管理与发行", "游戏市场与社区运营"])

    update("programming-languages-core", "补足一般 Android 应用与游戏平台 SDK 的对称边界；不把移动平台应用都推入游戏开发。",
           ["440c90896ecca986", "2f296046dc1d94b5"],
           description="编程语言参考，以及桌面、Web、Android 等通用应用开发；已有专业任务时优先对应专类。",
           include=["编程语言语法、运行时与内存管理", "桌面、Web、Android 等一般应用开发与软件设计"],
           exclude=["游戏平台 SDK、引擎专属工程与游戏玩法实现", "AI/数据工程、系统调试与安全、算法理论：归各自专类"],
           aliases=["编程语言参考", "通用应用开发"])

    update("systems-hardware-security", "勒索防护属于安全工程，原名称与边界过度限制为逆向许可分析。",
           ["d4ed25a12e2243cd", "2f296046dc1d94b5"],
           name="系统架构与网络安全",
           description="硬件与系统架构、嵌入式开发、逆向分析、系统调试及网络安全防御。",
           include=["CPU/GPU、嵌入式、逆向工程与 ADB 等系统调试", "漏洞防护、勒索软件应对、安全验证与安全运维"],
           exclude=["消费硬件导购与使用评测：归数码消费", "一般应用业务开发、普通软件分发、联网代理订购：归各自专类"],
           aliases=["系统与硬件架构", "安全工程与逆向分析"])

    update("dev-workflow-git", "未指明引擎的自定义编辑器扩展证据支持工程工具用途，不能仅凭标题推断 Unity 等品牌。",
           ["b506801a0fabf91e", "3e28c2fef32f57be"],
           description="代码与开发资产协作、开发环境、工程自动化、集成调试及通用编辑器扩展。",
           include=["版本控制、工程自动化、开发环境及未限定引擎的编辑器扩展", "HTTP/Webhook/邮件集成测试与调试"],
           exclude=["明确某一游戏引擎的编辑器工具：归该引擎", "普通浏览器插件、文档协作及网站运营分析：归相应工具类别"],
           aliases=["研发协作与版本控制", "开发者工程工具"])

    update("dev-technical-community", "社区和博客只是载体；明确主题的深页应按实际任务分类，仅综合主页保留在社区。",
           ["8729e3edabbad8d9", "0d964a4d8448ce0a", "98480e8f25d2f279"],
           description="没有更具体主任务的综合开发者论坛、技术博客主页与技术交流入口。",
           include=["跨多项编程主题的开发者论坛或博客主页", "综合技术交流与未限定专门技术任务的开发者经验入口"],
           exclude=["明确引擎、玩法、安全、工程工具或游戏制作管理的帖子和深页：按用途归类", "非技术主题的泛社交、艺术作品集与普通软件目录"],
           aliases=["综合技术社区", "开发者博客主页"])

    update("art-audio-media", "综合 2D/3D/UI/音频素材入口不能强制只选某一媒介；父类仅接收跨媒介综合资源，单一类型继续选子类。",
           ["085c64aeefebb584", "8c3e98efb9278de8", "e9eb2fec4d8d0d9f"],
           name="创作资源与素材", assignable=True,
           description="创作素材总类；直接归入仅限横跨 2D、3D、UI、音频等多种媒介的综合资源入口。",
           include=["跨两类以上创作媒介的综合素材库", "同时提供多种视觉/音频素材、模板及相关创作资源的总入口"],
           exclude=["明确单一 3D、音频或视觉素材用途：归相应子类", "以专业软件教学、作品展示、游戏代码或通用软件下载为主：归对应用途"],
           aliases=["综合创作素材", "跨媒介创作资源"])

    update("art-3d-assets", "把 3D 成品素材与音频、视觉素材放在同一个资源层；建模制作过程保留独立入口。",
           ["f8f2bb748786d4d0", "ad295a496e9fd5f4"],
           parent_id="art-audio-media",
           description="可直接使用的 3D 模型、PBR 材质、贴图纹理和 HDRI 成品素材。",
           include=["3D 模型、游戏场景与角色资产", "PBR 材质、贴图纹理和 HDRI 资源"],
           exclude=["建模、雕刻和渲染制作教学：归三维建模", "跨 2D/3D/音频综合总库：归创作资源与素材；纯视觉图片归视觉素材"],
           aliases=["3D模型与材质", "三维成品资产"])

    update("art-3d-software", "3D 素材迁移后原上级只剩一个子类，直接提升建模制作入口可消除无分支价值的容器。",
           ["776fb03692a58d2a", "f8f2bb748786d4d0"], parent_id="art-creative",
           aliases=["三维建模与雕刻", "三维动画与渲染制作"])
    removed = categories.pop("art-3d-modeling")
    candidate["categories"] = [item for item in candidate["categories"] if item["id"] != "art-3d-modeling"]
    changes.append({"operation": "remove", "category_id": "art-3d-modeling", "before": removed, "after": None,
                    "rationale": "子类分别归位到素材总类与美术根后删除冗余容器，不删除任何资源或其余类别 ID。",
                    "evidence": evidence(["776fb03692a58d2a", "f8f2bb748786d4d0"])})

    update("art-community-portfolio", "与技术社区对称：综合艺术社区/作品集保留此类，素材下载或制作教程深页优先实际用途。",
           ["2df2e56cb7395084", "085c64aeefebb584"],
           description="综合数字艺术社区、艺术家作品集、3D 展示与创作交流入口。",
           include=["综合 CG/原画/雕刻交流平台与艺术家作品集", "以浏览展示和创作交流为主要用途的 3D 作品平台"],
           exclude=["明确素材下载、建模教学或创意 AI 的页面：归对应专类", "程序技术论坛、泛社交或玩家游戏作品游玩入口"],
           aliases=["数字艺术作品集", "艺术创作社区"])

    update("single-purpose-utilities", "补足通用软件目录、APK 分发与 Firefox 使用/插件等常见工具用途；专业用途仍保留专属入口。",
           ["67bc9d4e3752cb9c", "7ff709b2ce570d2c", "ca06a742819c38d0", "5f1242bb0c4960df"],
           name="通用软件与实用工具",
           description="通用软件发现、下载和日常使用，以及未有专属类别的轻量实用工具。",
           include=["综合软件目录、APK/应用分发、浏览器使用帮助与通用插件", "二维码、格式转换、字幕提取、临时邮箱、字体识别与专注工具"],
           exclude=["专业开发、设计创作、金融或语言学习用途：归对应专类", "文档协作、地图、网站/App 运营、AI 助手：归已有工具专类"],
           aliases=["通用软件与应用", "日常实用工具"])

    update("productivity-online-tools", "子类增加桌面/移动通用软件后，上级名称不应只限定在线处理；只调整容器描述，不使其直接可分配。",
           ["7ff709b2ce570d2c", "ca06a742819c38d0", "d72f901a1384732a"],
           name="软件与效率工具",
           description="通用软件与实用工具、文档协作存储，以及网站和应用运营工具。",
           include=["通用软件下载、使用及日常数据处理", "文档协作存储与网站/App 运营工具"],
           exclude=["专业开发、创作与金融任务工具", "独立列出的 AI 助手、地图、通信与托管服务"],
           aliases=["软件与通用效率工具"])

    update("general-books-encyclopedia", "跨主题课程平台与图书总入口需要合法位置；具体专业课程仍按主题，避免学习成为承接全部教程的兜底。",
           ["7c9601f620f95ca0", "420e2921195ef216"],
           name="综合学习资源与通识阅读",
           description="跨多个专业的课程或图书总入口，以及通识百科、书摘和通识阅读资源。",
           include=["跨专业综合课程平台、教学资源总目录与图书库", "通识百科、书单书摘和非专业通识阅读"],
           exclude=["明确专业主题的课程或资源页：按开发、美术、金融等用途归类", "科研论文检索、统考备考和外语训练：归各自专类"],
           aliases=["综合课程与图书入口", "通识阅读与百科"])

    update("academic-library", "综合课程子类不能藏在只称开放图书的父级，同步修正上级措辞与范围。",
           ["7c9601f620f95ca0"],
           name="文献检索与综合学习资源",
           description="学术文献检索和跨专业课程、图书、百科的综合入口。",
           include=["科研论文检索与引文资源", "跨专业课程、图书与通识百科入口"],
           exclude=["明确单一专业主题的课程与教程", "统考备考、外语训练与即时新闻"],
           aliases=["文献与综合学习入口"])

    language = {"id": "foreign-language-learning", "parent_id": "learning-reference", "name": "外语学习",
                "description": "外语发音、听说、词汇、语法与真实语境训练。",
                "include": ["外语发音、听力和口语训练", "词汇语法学习及真实视频语料检索"],
                "exclude": ["仅做文本翻译或格式处理的通用工具", "专业课程即使使用外语授课也按其专业用途归类"],
                "aliases": ["外语听说与发音", "外语学习"], "assignable": True}
    candidate["categories"].append(language)
    categories[language["id"]] = language
    changes.append({"operation": "add", "category_id": language["id"], "before": None, "after": copy.deepcopy(language),
                    "rationale": "Youglish 具有明确外语发音训练用途，v3 无对应类别；建立用途分支而不是品牌目录。",
                    "evidence": evidence(["420e2921195ef216"])})
    update("learning-reference", "纳入新增外语学习与综合课程入口，专业用途优先的总体边界不变。",
           ["420e2921195ef216", "7c9601f620f95ca0"],
           description="文献检索、综合学习与通识阅读、外语训练、统考备考及数理经济基础课程。",
           include=["科研文献与综合课程图书入口", "外语、升学备考及数理经济与计算机理论学习"],
           exclude=["明确开发、美术、投资等实践用途的专业教程", "即时新闻、商业运营与基础设施服务"],
           aliases=["学习与资料", "学术与通识学习"])

    update("cloud-vps-hosting", "开服托管仍是服务器服务，不因服务游戏而变成联网代码开发。",
           ["8327260ffdb3c4ca"],
           description="云主机、远程计算、网站托管和游戏服务器租赁开服管理。",
           include=["VPS、远程开发机、网站及容器托管服务", "游戏服务器租赁、开服与托管控制面板"],
           exclude=["多人网络同步框架与代码：归游戏联机", "节点代理订阅与通用网盘存储：归相应服务"],
           aliases=["云主机与服务器托管", "远程计算环境"])

    update("game-downloads-consoles", "玩家购买、试玩、下载与独立作品目录都属于游玩入口；目录不应只覆盖单机资源站。",
           ["7292d666d261031f", "e13890814d826fd4", "9bdb42a8333f2940"],
           name="玩家游戏资源与商店",
           description="面向玩家的游戏购买、试玩下载、独立游戏作品入口、攻略和游玩评测。",
           include=["正规游戏商店、游戏捆绑包及独立作品购买或游玩入口", "玩家游戏下载、攻略、主机资源、账号服务与游玩评测"],
           exclude=["专业游戏产业数据、制作管理与发行营销：归游戏项目运营", "模组制作、游戏代码和创作素材：按具体开发或创作用途归类"],
           aliases=["游戏商店与玩家资源", "游戏游玩与攻略"])

    update("crossborder-telecom-sim", "境外手机卡、eSIM 与保号按通信任务统一，不与跨境生活重复收纳。",
           ["7aa8f979081cc10b", "f89b8de8a6546bf6"],
           description="境外实体卡、旅行 eSIM、虚拟电话及号码购买、使用和保号服务。",
           include=["境外实体手机卡、旅行 eSIM 与虚拟电话号码", "海外号码充值、激活、保号及通信使用指南"],
           exclude=["境外银行开户与信用卡：归跨境银行生活", "VPN 代理、服务器托管和普通设备使用：归各自专类"],
           aliases=["境外号卡与保号", "eSIM与虚拟通信"])

    update("crossborder-banking-living", "删除手机卡保号的重叠收纳，保留银行账户、信用卡和具体跨境生活办事用途。",
           ["7aa8f979081cc10b", "151d866e786cfe41"],
           include=["境外银行开户、账户费率、信用卡权益与使用指南", "跨境生活的账单、身份资料和办事辅助"],
           exclude=["境外手机号卡、eSIM 与保号：归跨境通信", "第三方虚拟卡代充、证券披露与投资研究：归对应专类"],
           aliases=["境外银行与信用卡", "跨境生活办事"])

    update("global-business-media", "股票、外汇与 Crypto 同时出现可以形成一致的综合财经新闻用途，不必误判为互相冲突。",
           ["bb7ddd98ec291593"],
           description="综合商业财经媒体和行业新闻报道，可同时涵盖股票、外汇、加密资产与宏观经济。",
           include=["跨股票、外汇、Crypto 等市场的综合财经新闻与评论", "商业管理、科技商业与行业新闻报道"],
           exclude=["纯加密垂直资讯：归加密资讯；主用途是专业游戏销量数据：归游戏项目运营", "法定披露、量化数据接口及系统投资教育：归对应金融类别"],
           aliases=["综合财经新闻", "商业与行业新闻"])

    update("finance-research-asset-mgmt", "通用投资入门与经纪商教育属于明确金融用途，不因课程形式被送回综合学习入口。",
           ["b3116ebfb9631e23", "356b81101f66d5df"],
           description="投资研究、资产管理、证券市场分析以及通用投资基础与个人理财教育。",
           include=["资产管理机构、投资研究与市场分析", "投资基础、证券交易常识和经纪商投资教育"],
           exclude=["衍生品定价、量化策略和数学基础：归各自专类", "综合财经新闻、法定披露及银行开户：归对应类别"],
           aliases=["投资研究与理财教育", "资产管理与投资基础"])

    update("web-analytics-seo", "Screenshot Studio 明确用于普通 App 商店宣传，不能根据 App、开发者或商店字样推断为游戏工具。",
           ["d72f901a1384732a"],
           name="网站与应用运营",
           description="通用网站和应用的流量分析、SEO/ASO、运营监测与推广素材工具。",
           include=["网站流量、SEO、竞争分析与运营变更监测", "普通 App 的 ASO、商店截图生成及推广工具"],
           exclude=["明确针对游戏的市场数据、发行与商店优化：归游戏项目运营", "HTTP/Webhook 开发调试、一般绘图素材或应用代码：按对应用途归类"],
           aliases=["网站与App运营工具", "SEO与ASO推广"])

    original_rules = copy.deepcopy(candidate["rules"])
    candidate["rules"] = [
        "按具体页面主用途归类，专业领域优先于网站品牌、社区/博客/视频/课程载体和旧文件夹；旧目录只作弱线索，不补写证据。",
        "有明确用途的社区帖子和博客深页归对应专类；只有综合开发者主页归技术社区、综合艺术展示主页归艺术社区，非专业社群才归社交。",
        "游戏任务优先级：制作管理/发行运营 > 联机同步 > 玩法子系统 > 一般引擎工程；独立渲染算法归底层图形，明确引擎工具归该引擎，未限定引擎的编辑器扩展归工程效能。",
        "游戏引擎与平台开发父类仅直接接收跨引擎、Android 游戏 SDK 和未设子类的 2D/Web 引擎；Unity/UE/底层图形选子类，一般 Android 应用归应用开发。",
        "创作资源父类仅接收跨媒介综合总库；单一 3D、音频或视觉素材选子类，建模制作教程、作品展示、游戏代码各归对应用途。",
        "游戏项目运营必须有明确游戏证据；普通 App 截图宣传、ASO 和网站运营工具归网站与应用运营，不能将 App 等同于游戏。玩家商店与独立作品游玩入口归娱乐。",
        "综合课程或图书总入口归综合学习；明确专业课程按用途，外语听说归外语学习。概率统计等基础理论归数理，投资入门归投研，定价/量化应用归量化金融。",
        "综合财经媒体跨股票、外汇和 Crypto 是统一新闻用途；纯 Crypto 新闻归加密资讯，证券官方披露归披露，投资教育与研究归投研。",
        "Web3 开发基础设施与授权安全、DeFi 交易借贷、链上行情分析和加密新闻分别按对应专类区分。",
        "普通软件目录、APK、浏览器使用与通用插件归通用软件；明确开发、创作、金融或外语用途的专业工具优先各自专类。",
        "海外手机卡/eSIM/保号统一归跨境通信；境外银行与生活办事归生活；游戏开服托管归云主机，不归游戏联网代码。",
        "AI 原理课程归算法基础，生产 API/SDK、OCR、MCP 与数据工程归 AI 开发，普通助手归工具，创作产品归 AI 创意。LaTeX 排版归文档，地图/GIS 工具归地理信息。",
        "优先最具体且证据支持的可分配类别；父类仅在其明确接收条件成立时直接分配。主题证据不足、真正冲突或仍缺类别时诚实 needs_review，不自建目录。",
    ]
    previous_version = candidate["version"]
    candidate["version"] = 4
    previous_reference = copy.deepcopy(candidate["reference_summary"])
    candidate["reference_summary"] = [*source["reference_summary"],
        "第四版为宿主根据真实全库分类的覆盖缺口与边界问题整理的候选；尚未重新分类验证，不代表生产批准。"]
    global_changes = {"version": {"before": previous_version, "after": 4},
                      "rules": {"before": original_rules, "after": candidate["rules"]},
                      "reference_summary": {"before": previous_reference, "after": candidate["reference_summary"]}}
    return candidate, changes, global_changes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skill", type=Path, default=runner.DEFAULT_SKILL)
    args = parser.parse_args(argv)
    directory = args.run_dir.resolve()
    source_file = args.source or directory / "taxonomy-v3.json"
    output_file = args.output or directory / "taxonomy-v4-draft.json"
    provenance_file = output_file.with_name(output_file.stem + "-provenance.json")
    validator = runner.load_validator(args.skill)
    source = validator.read_json(source_file)
    if source["version"] != 3:
        raise ValueError("This evidence-specific refinement expects source version 3")
    before_info = validator.taxonomy(source)
    resources = validator.resources(validator.read_json(directory / "resources.json"))
    assignment_data = validator.read_json(directory / "assignments-v3.json")
    validator.assignments(source, {"resources": list(resources.values())}, assignment_data, before_info["taxonomy_hash"])
    assignments = {item["resource_id"]: item for item in assignment_data["items"]}
    candidate, changes, global_changes = refine(source, resources, assignments)
    after_info = validator.taxonomy(candidate)
    old_ids = {item["id"] for item in source["categories"]}
    new_ids = {item["id"] for item in candidate["categories"]}
    if old_ids - new_ids != {"art-3d-modeling"} or new_ids - old_ids != {"foreign-language-learning"}:
        raise ValueError("Unexpected category ID removal or addition")
    for change in changes:
        for citation in change["evidence"]:
            if citation["quote"] not in resources[citation["resource_id"]][citation["field"]]:
                raise ValueError("Nonliteral source citation")
    provenance = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "host_authored_candidate_pending_API_evaluation", "production_approved": False,
        "api_called": False, "business_data_modified": False,
        "authorization_basis": "User requested API-based bookmark classification, inspection and iterative optimization; this artifact is a reviewable draft, not production approval.",
        "source_file": str(source_file.resolve()), "source_taxonomy_hash": before_info["taxonomy_hash"],
        "candidate_file": str(output_file.resolve()), "candidate_taxonomy_hash": after_info["taxonomy_hash"],
        "source_assignments_file": str((directory / "assignments-v3.json").resolve()),
        "source_assignments_sha256": runner.digest(assignment_data),
        "source_resources_sha256": runner.digest({"resources": list(resources.values())}),
        "script_sha256": __import__("hashlib").sha256(Path(__file__).read_bytes()).hexdigest(),
        "before_counts": before_info["counts"], "after_counts": after_info["counts"],
        "category_change_count": len(changes),
        "added_ids": sorted(new_ids - old_ids), "removed_ids": sorted(old_ids - new_ids),
        "retained_existing_ids": sorted(old_ids & new_ids),
        "changes": changes, "global_changes": global_changes,
        "validation": {"structural_validation": "passed", "literal_source_citations": "passed",
                       "semantic_quality": "Not yet measured for this candidate; structural validity is not semantic accuracy."},
        "limitations": [
            "These changes respond to identified coverage/boundary observations; they do not assign or approve any resource.",
            "Original titles identify cases and quoted evidence. V3 model reasons are observations, not authoritative expected answers.",
            "No independent holdout expectation labels were read by this script.",
            "Ambiguous evidence and remaining genuine taxonomy gaps may still require human review.",
        ],
    }
    runner.write_json(output_file, candidate)
    runner.write_json(provenance_file, provenance)
    print(json.dumps({"candidate": str(output_file), "provenance": str(provenance_file),
                      "changes": len(changes), "counts": after_info["counts"],
                      "taxonomy_hash": after_info["taxonomy_hash"], "production_approved": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
