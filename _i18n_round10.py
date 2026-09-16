"""Round-10: Final i18n polish - add remaining locale keys and fix hardcoded strings"""
import os, re, json

root = r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统'
zh_path = os.path.join(root, 'locales', 'zh-CN.json')
en_path = os.path.join(root, 'locales', 'en-US.json')
html_path = os.path.join(root, 'app', 'templates', 'index.html')

# Load locale files
with open(zh_path, 'r', encoding='utf-8') as f:
    zh = json.load(f)
with open(en_path, 'r', encoding='utf-8') as f:
    en = json.load(f)

# Keys to add (key_path, zh_val, en_val)
new_keys = [
    ('navHome', '项目中心', 'Home'),
    ('navUpload', '上传小说', 'Upload Novel'),
    ('navAuto', '自动生产', 'Auto Prod.'),
    ('navDeliver', '成品验收', 'Deliverables'),
    ('navChars', '角色管理', 'Characters'),
    ('navRelation', '关系图谱', 'Relations'),
    ('navGrid', '九宫格分镜', 'Storyboard'),
    ('navExport', '导出项目', 'Export'),
    ('navMemory', 'AI 记忆', 'AI Memory'),
    ('navSettings', 'AI 设置', 'Settings'),
    ('common.developing', '功能开发中...', 'Developing...'),
    ('common.promptCopied', '提示词已复制', 'Prompt copied'),
    ('common.relationAdded', '关系已添加', 'Relation added'),
    ('common.relationDeleted', '关系已删除', 'Relation deleted'),
    ('common.relationAddFailed', '添加失败', 'Add failed'),
    ('common.relationDeleteFailed', '删除失败', 'Delete failed'),
    ('common.selectTwoChars', '请选择两个角色', 'Select two characters'),
    ('common.sameCharError', '不能选择同一个角色', 'Cannot select same character'),
    ('common.deleteRelationConfirm', '确定删除此关系？', 'Delete this relation?'),
    ('common.noRelation', '暂无关系', 'No relations'),
    ('common.noRelationTip', '暂无角色关系，请先添加角色或创建关系', 'No character relations yet'),
    ('common.roleMain', '主角', 'Main'),
    ('common.roleSupport', '配角', 'Support'),
    ('deliver.acceptedLabel', '已验收', 'Approved'),
    ('deliver.acceptedMeta', '已验收', 'Approved'),
    ('deliver.report', '生产报告', 'Production Report'),
    ('character.editTitle', '编辑角色', 'Edit Character'),
]

# Add to both locales
for key_path, zh_val, en_val in new_keys:
    parts = key_path.split('.')
    d_zh, d_en = zh, en
    for p in parts[:-1]:
        if p not in d_zh:
            d_zh[p] = {}
        if p not in d_en:
            d_en[p] = {}
        d_zh, d_en = d_zh[p], d_en[p]
    d_zh[parts[-1]] = zh_val
    d_en[parts[-1]] = en_val

zh_total = sum(len(v) if isinstance(v, dict) else 1 for v in zh.values())
en_total = sum(len(v) if isinstance(v, dict) else 1 for v in en.values())
print('Added %d new keys' % len(new_keys))
print('zh-CN total: %d' % zh_total)
print('en-US total: %d' % en_total)

# Save locale files
with open(zh_path, 'w', encoding='utf-8') as f:
    json.dump(zh, f, ensure_ascii=False, indent=2)
    f.write('\n')
with open(en_path, 'w', encoding='utf-8') as f:
    json.dump(en, f, ensure_ascii=False, indent=2)
    f.write('\n')
print('Locale files saved')

# Now fix HTML
with open(html_path, 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Fix nav title attributes -> data-i18n
nav_titles = [
    ('title="项目中心"', 'data-i18n="navHome" title="{{navHome}}"'),
    ('title="上传小说"', 'data-i18n="navUpload" title="{{navUpload}}"'),
    ('title="自动生产"', 'data-i18n="navAuto" title="{{navAuto}}"'),
    ('title="成品验收"', 'data-i18n="navDeliver" title="{{navDeliver}}"'),
    ('title="角色管理"', 'data-i18n="navChars" title="{{navChars}}"'),
    ('title="关系图谱"', 'data-i18n="navRelation" title="{{navRelation}}"'),
    ('title="九宫格分镜"', 'data-i18n="navGrid" title="{{navGrid}}"'),
    ('title="导出项目"', 'data-i18n="navExport" title="{{navExport}}"'),
    ('title="AI 记忆"', 'data-i18n="navMemory" title="{{navMemory}}"'),
    ('title="AI 设置"', 'data-i18n="navSettings" title="{{navSettings}}"'),
]
for old, new in nav_titles:
    count = html.count(old)
    if count:
        html = html.replace(old, new)
        print('  Nav title replaced: %dx' % count)

# 2. Fix JS toast/confirm strings
replacements = [
    ("toast('功能开发中...','wn')", "toast(t('common.developing'),'wn')"),
    ("toast('上传参考图功能开发中...','wn')", "toast(t('common.developing'),'wn')"),
    ("toast('提示词已复制','ok')", "toast(t('common.promptCopied'),'ok')"),
    ("toast('关系已添加','ok')", "toast(t('common.relationAdded'),'ok')"),
    ("toast('关系已删除','ok')", "toast(t('common.relationDeleted'),'ok')"),
    ("toast('请选择两个角色','wn')", "toast(t('common.selectTwoChars'),'wn')"),
    ("toast('不能选择同一个角色','wn')", "toast(t('common.sameCharError'),'wn')"),
    ("if(!confirm('确定删除此关系？'))return;", "if(!confirm(t('common.deleteRelationConfirm')))return;"),
    ("'<div class=\"empty\">暂无关系</div>'", "'<div class=\"empty\">'+t('common.noRelation')+'</div>'"),
    ("toast('添加失败: '+(d.error||''), 'er')", "toast(t('common.relationAddFailed')+': '+(d.error||''), 'er')"),
    ("toast('删除失败: '+(d.error||''), 'er')", "toast(t('common.relationDeleteFailed')+': '+(d.error||''), 'er')"),
    ("toast('编辑角色: '+id,'wn')", "toast(t('character.editTitle')+': '+id,'wn')"),
    ("> 生产报告</div>", "'> '+t('deliver.report')+'</div>"),
    ("innerHTML='<span class=\"status status-done\"><i class=\"bi bi-check\"></i> 已验收</span>'",
     "innerHTML='<span class=\"status status-done\"><i class=\"bi bi-check\"></i> '+t('deliver.acceptedLabel')+'</span>'"),
    ("toast('已验收','ok')", "toast(t('deliver.acceptedLabel'),'ok')"),
    ("|| 'AI 记忆'", "|| t('memory.title')"),
]
for old, new in replacements:
    count = html.count(old)
    if count:
        html = html.replace(old, new)
        print('  JS fix: %dx - %s' % (count, old[:40]))

# 3. Fix 主角 check (role comparison) - use separate vars for zh/en
html = html.replace(
    "node.role==='主角'?",
    "node.role===__roleMain?"
)
html = html.replace(
    "c.role==='主角'?",
    "c.role===__roleMain?"
)
# Add __roleMain variable at start of script
html = html.replace('<script>', '<script>\nvar __roleMain = t(\'common.roleMain\');\nvar __roleSupport = t(\'common.roleSupport\');')

# 4. Fix deliverable-meta line with interpolation
# Original: '集 · 已验收: '+(d.review==='approved'?'✓':t('deliver.pending'))
html = html.replace(
    "集 · 已验收: '+(d.review==='approved'?'✓':t('deliver.pending'))",
    "t('deliver.verified', {n: d.episode_no, s: d.review==='approved'?'✓':t('deliver.pending')})"
)

# 5. Fix relationGraph empty state (curLang ternary)
old_rel = "(curLang==='zh-CN'?'暂无角色关系，请先添加角色':'"
if old_rel in html:
    # This is tricky - need to handle bilingual fallback
    # Replace with just the zh version since we're using t() now
    html = html.replace(old_rel, "t('common.noRelationTip')")
    print('  Fixed relation tip ternary')

# 6. Fix character prompt copy
html = html.replace(
    "navigator.clipboard.writeText('角色提示词内容');",
    "navigator.clipboard.writeText(t('character.promptContent'));"
)

# 7. Fix nine-grid toast lines
html = html.replace(
    "toast('已选择 '+d.shot.composition+' 构图','ok')",
    "toast(t('nineGrid.selectComposition', {comp: d.shot.composition})||('已选择 '+d.shot.composition+' 构图'),'ok')"
)
html = html.replace(
    "toast('选择失败: '+(d.error||''), 'er')",
    "toast(t('nineGrid.selectFailed')+': '+(d.error||''), 'er')"
)

# Save
with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html)
print('HTML saved')

# Verify
with open(html_path, 'r', encoding='utf-8') as f:
    final_html = f.read()

remaining = []
for line in final_html.splitlines():
    stripped = line.strip()
    if 'data-i18n' in stripped or stripped.startswith('//') or stripped.startswith('/*') or stripped.startswith('<!--'):
        continue
    if re.search(r'[\u4e00-\u9fff]{2,}', stripped):
        remaining.append((len(remaining)+1, stripped[:80]))

print('\nFinal stats:')
print('  data-i18n attrs: %d' % final_html.count('data-i18n'))
print('  t() calls: %d' % final_html.count("t('"))
print('  Remaining Chinese lines: %d' % len(remaining))
for ln, txt in remaining[:15]:
    print('    %d: %s' % (ln, txt))
