# -*- coding: utf-8 -*-
"""Complete i18n fix - batch replacement for all remaining hardcoded strings"""
import re

file_path = r"C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\app\templates\index.html"

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

replacements = [
    # Title and subtitle
    ('<div class="topbar-title" id="pageTitle">项目中心</div>', '<div class="topbar-title" id="pageTitle" data-i18n="project.title">项目中心</div>'),
    ('<div class="topbar-sub" id="pageSub">选择项目开始创作，或上传新小说</div>', '<div class="topbar-sub" id="pageSub" data-i18n="project.chooseOrUpload">选择项目开始创作，或上传新小说</div>'),
    
    # Modal titles
    ('<div class="modal-title"><i class="bi bi-gear"></i> AI 模型设置</div>', '<div class="modal-title"><i class="bi bi-gear"></i> <span data-i18n="settings.title">AI 模型设置</span></div>'),
    ('<div class="modal-title"><i class="bi bi-people"></i> 角色管理</div>', '<div class="modal-title"><i class="bi bi-people"></i> <span data-i18n="character.title">角色管理</span></div>'),
    ('<div class="modal-title"><i class="bi bi-ui-checks"></i> 九宫格分镜</div>', '<div class="modal-title"><i class="bi bi-ui-checks"></i> <span data-i18n="nineGrid.title">九宫格分镜</span></div>'),
    ('<div class="modal-title"><i class="bi bi-box-arrow-up"></i> 导出项目</div>', '<div class="modal-title"><i class="bi bi-box-arrow-up"></i> <span data-i18n="export.title">导出项目</span></div>'),
    ('<div class="modal-title"><i class="bi bi-diagram-3"></i> 角色关系图谱</div>', '<div class="modal-title"><i class="bi bi-diagram-3"></i> <span data-i18n="relation.title">角色关系图谱</span></div>'),
    
    # Select options for character role
    ('<option>主角</option><option>配角</option><option>反派</option><option>NPC</option>', 
     '<option data-i18n="character.roleProtagonist">主角</option><option data-i18n="character.roleSupporting">配角</option><option data-i18n="character.roleVillain">反派</option><option data-i18n="character.roleNPC">NPC</option>'),
    
    # Relation select options
    ('<option value="">选择角色A...</option>', '<option value="" data-i18n="relation.selectCharA">选择角色A...</option>'),
    ('<option value="">选择角色B...</option>', '<option value="" data-i18n="relation.selectCharB">选择角色B...</option>'),
    ('<option value="family">亲属</option>', '<option value="family" data-i18n="relation.types.family">亲属</option>'),
    ('<option value="friend">朋友</option>', '<option value="friend" data-i18n="relation.types.friend">朋友</option>'),
    ('<option value="enemy">敌人</option>', '<option value="enemy" data-i18n="relation.types.enemy">敌人</option>'),
    ('<option value="romance">恋人</option>', '<option value="romance" data-i18n="relation.types.romance">恋人</option>'),
    ('<option value="mentor">师徒</option>', '<option value="mentor" data-i18n="relation.types.mentor">师徒</option>'),
    ('<option value="colleague">同事</option>', '<option value="colleague" data-i18n="relation.types.colleague">同事</option>'),
    ('<option value="rival">对手</option>', '<option value="rival" data-i18n="relation.types.rival">对手</option>'),
    ('<option value="ally">盟友</option>', '<option value="ally" data-i18n="relation.types.ally">盟友</option>'),
    ('<option value="stranger">陌生人</option>', '<option value="stranger" data-i18n="relation.types.stranger">陌生人</option>'),
    ('<option value="master">主仆</option>', '<option value="master" data-i18n="relation.types.master">主仆</option>'),
    
    # Strength options
    ('<option value="close">亲密</option>', '<option value="close" data-i18n="relation.strengths.close">亲密</option>'),
    ('<option value="strong">强烈</option>', '<option value="strong" data-i18n="relation.strengths.strong">强烈</option>'),
    ('<option value="medium" selected>中等</option>', '<option value="medium" selected data-i18n="relation.strengths.medium">中等</option>'),
    ('<option value="weak">微弱</option>', '<option value="weak" data-i18n="relation.strengths.weak">微弱</option>'),
    ('<option value="neutral">中性</option>', '<option value="neutral" data-i18n="relation.strengths.neutral">中性</option>'),
    ('<option value="hostile">敌对</option>', '<option value="hostile" data-i18n="relation.strengths.hostile">敌对</option>'),
    ('<option value="enemy">仇敌</option>', '<option value="enemy" data-i18n="relation.strengths.enemy">仇敌</option>'),
    
    # Help text
    ('FCPXML 用于 Final Cut Pro，EDL 用于 Premiere/达芬奇', '<span data-i18n="export.desc">FCPXML 用于 Final Cut Pro，EDL 用于 Premiere/达芬奇</span>'),
    
    # Dynamic JS text replacements
    ("+' 集'", "+'<span data-i18n=\\"auto.episodeSuffix\\">集</span>'"),
    ("?'运行中':'已停止')+", "?'"+t('auto.state.working')+"':'"+t('auto.state.paused')+"')"),
    ("'已完成'", "'"+t('auto.epState.done')+"'"),
    ("'失败'", "'"+t('analytics.failed')+"'"),
    ("'轮转次数'", "'"+t('auto.retries')+"'"),
    ("'生产环境检查'", "'"+t('auto.check.allOk')+"'"),
    ("'项目列表 '", "'"+t('auto.plans')+"' "),
    ("+projList.length+' 个项目'", "+projList.length+' <span data-i18n=\\"auto.enabledProjects\\">项目</span>'"),
    ("' 生产中'", "t('auto.producing')"),
    ("'风格: '", "t('auto.style')+': '"),
    ("' 集数进度'", "t('auto.epProgress')"),
    ("'重试 '", "t('auto.retryN').replace('{n}', 'ep.retries)+' ')"),
    ("' 交付物'", "t('deliver.title')"),
    ("'第'+d.episode_no+'集'", "t('auto.epN').replace('{n}', d.episode_no)"),
    ("' 下载'", "t('common.download')"),
    ("' 全部成品'", "t('auto.deliver')"),
    ("'还没有成品'", "t('auto.deliverEmpty')"),
    ("'启动全自动生产后，成品会出现在这里'", "t('auto.deliverHint')"),
    ("' 播放'", "t('auto.play')"),
    ("' 生产报告'", "t('auto.analytics')"),
    ("' 已验收'", "t('deliver.accepted')"),
    ("'还没有角色，请先上传小说生成剧本'", "t('character.noChars')"),
    ("'点击选择最佳构图，系统将基于该构图生成关键帧'", "t('nineGrid.selectBest')"),
    ("'暂无关系'", "t('relation.noRelations')"),
]

print("Applying batch i18n fixes...")
count = 0
for old, new in replacements:
    if old in content:
        content = content.replace(old, new)
        count += 1
        print(f"✓ {old[:40]}...")
    else:
        print(f"✗ NOT FOUND: {old[:40]}...")

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"\nApplied {count} replacements!")
