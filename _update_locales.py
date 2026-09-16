# -*- coding: utf-8 -*-
"""Update locale files with new keys for relation graph, nine-grid, export, character."""
import json

paths = {
    'zh': r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\locales\zh-CN.json',
    'en': r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\locales\en-US.json',
}

with open(paths['zh'], 'r', encoding='utf-8') as f:
    zh = json.load(f)
with open(paths['en'], 'r', encoding='utf-8') as f:
    en = json.load(f)

# Character management
zh['character'] = {
    'title': '角色管理',
    'subtitle': '管理角色资产与一致性参考图',
    'name': '角色名称',
    'role': '角色类型',
    'add': '添加',
    'edit': '编辑',
    'uploadRef': '上传参考图',
    'copyPrompt': '复制提示词',
    'noChars': '还没有角色，请先上传小说生成剧本',
    'back': '返回',
    'promptCopied': '提示词已复制',
    'uploadRefDeveloping': '上传参考图功能开发中...',
    'addSuccess': '角色添加成功',
    'addFailed': '添加失败',
    'selectChar': '请选择角色',
    'sameChar': '不能选择同一个角色',
}

zh['relation'] = {
    'title': '角色关系图谱',
    'subtitle': '查看和管理角色之间的关系网络',
    'selectCharA': '选择角色A...',
    'selectCharB': '选择角色B...',
    'type': '关系类型',
    'strength': '关系强度',
    'addRelation': '添加',
    'deleteRelation': '删除',
    'noRelations': '暂无关系',
    'noChars': '暂无角色关系，请先添加角色',
    'addSuccess': '关系已添加',
    'addFailed': '添加失败',
    'deleteSuccess': '关系已删除',
    'deleteConfirm': '确定删除此关系？',
    'deleteFailed': '删除失败',
    'loadFailed': '加载失败',
    'relations': '关系',
    'types': {
        'family': '亲属', 'friend': '朋友', 'enemy': '敌人',
        'romance': '恋人', 'mentor': '师徒', 'colleague': '同事',
        'rival': '对手', 'ally': '盟友', 'stranger': '陌生人', 'master': '主仆',
    },
    'strengths': {
        'close': '亲密', 'strong': '强烈', 'medium': '中等',
        'weak': '微弱', 'neutral': '中性', 'hostile': '敌对', 'enemy': '仇敌',
    },
}

zh['nineGrid'] = {
    'title': '九宫格分镜',
    'subtitle': '生成9种构图候选，选择最优镜头',
    'sceneDesc': '输入场景描述...',
    'generate': '生成',
    'selectBest': '点击选择最佳构图，系统将基于该构图生成关键帧',
    'generateSuccess': '生成成功',
    'generateFailed': '生成失败',
    'selectSuccess': '已选择',
    'selectFailed': '选择失败',
}

zh['export'] = {
    'title': '导出项目',
    'subtitle': '导出为专业后期格式',
    'fcpxml': 'FCPXML',
    'edl': 'EDL',
    'json': 'JSON',
    'desc': 'FCPXML 用于 Final Cut Pro，EDL 用于 Premiere/达芬奇',
    'exportSuccess': '已开始下载',
    'exportFailed': '导出失败',
}

en['character'] = {
    'title': 'Character Management',
    'subtitle': 'Manage character assets and consistency references',
    'name': 'Character Name',
    'role': 'Role Type',
    'add': 'Add',
    'edit': 'Edit',
    'uploadRef': 'Upload Reference',
    'copyPrompt': 'Copy Prompt',
    'noChars': 'No characters yet. Upload a novel to generate scripts first.',
    'back': 'Back',
    'promptCopied': 'Prompt copied',
    'uploadRefDeveloping': 'Upload reference feature in development...',
    'addSuccess': 'Character added successfully',
    'addFailed': 'Failed to add',
    'selectChar': 'Please select a character',
    'sameChar': 'Cannot select the same character',
}

en['relation'] = {
    'title': 'Character Relations',
    'subtitle': 'View and manage relationship network between characters',
    'selectCharA': 'Select Character A...',
    'selectCharB': 'Select Character B...',
    'type': 'Relation Type',
    'strength': 'Strength',
    'addRelation': 'Add',
    'deleteRelation': 'Delete',
    'noRelations': 'No relations yet',
    'noChars': 'No character relations. Please add characters first.',
    'addSuccess': 'Relation added',
    'addFailed': 'Failed to add relation',
    'deleteSuccess': 'Relation deleted',
    'deleteConfirm': 'Delete this relation?',
    'deleteFailed': 'Failed to delete',
    'loadFailed': 'Failed to load',
    'relations': 'Relations',
    'types': {
        'family': 'Family', 'friend': 'Friend', 'enemy': 'Enemy',
        'romance': 'Romance', 'mentor': 'Mentor', 'colleague': 'Colleague',
        'rival': 'Rival', 'ally': 'Ally', 'stranger': 'Stranger', 'master': 'Master/Servant',
    },
    'strengths': {
        'close': 'Close', 'strong': 'Strong', 'medium': 'Medium',
        'weak': 'Weak', 'neutral': 'Neutral', 'hostile': 'Hostile', 'enemy': 'Enemy',
    },
}

en['nineGrid'] = {
    'title': 'Nine-Grid Storyboard',
    'subtitle': 'Generate 9 composition candidates, select the best shot',
    'sceneDesc': 'Enter scene description...',
    'generate': 'Generate',
    'selectBest': 'Click to select the best composition. The system will generate keyframes based on your selection.',
    'generateSuccess': 'Generated successfully',
    'generateFailed': 'Generation failed',
    'selectSuccess': 'Selected',
    'selectFailed': 'Selection failed',
}

en['export'] = {
    'title': 'Export Project',
    'subtitle': 'Export to professional post-production formats',
    'fcpxml': 'FCPXML',
    'edl': 'EDL',
    'json': 'JSON',
    'desc': 'FCPXML for Final Cut Pro, EDL for Premiere/DaVinci',
    'exportSuccess': 'Download started',
    'exportFailed': 'Export failed',
}

with open(paths['zh'], 'w', encoding='utf-8') as f:
    json.dump(zh, f, ensure_ascii=False, indent=2)
with open(paths['en'], 'w', encoding='utf-8') as f:
    json.dump(en, f, ensure_ascii=False, indent=2)

print('Locale files updated successfully')
