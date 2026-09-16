"""Fix duplicate routes in app.py by removing the second occurrence of each"""
import re

path = r'C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\app\app.py'

with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find all duplicate routes
route_lines = []
for i, line in enumerate(lines, 1):
    m = re.search(r"@app\.route\('([^']+)'", line)
    if m:
        route_lines.append((m.group(1), i))

from collections import defaultdict
route_map = defaultdict(list)
for route, line_no in route_lines:
    route_map[route].append(line_no)

duplicates = {r: ls for r, ls in route_map.items() if len(ls) > 1}
print(f'Found {len(duplicates)} duplicate routes:')
for r, ls in sorted(duplicates.items()):
    print(f'  {r}: L{ls[0]}, L{ls[1]}')

# For each duplicate, find and remove the second occurrence + its function body
# We need to find where each duplicate function starts and ends

# Strategy: Find all line numbers to remove (second occurrence of each duplicate route)
lines_to_remove = set()
for route, line_nums in duplicates.items():
    # Keep the first occurrence, mark the second for removal
    second_line = line_nums[1]
    lines_to_remove.add(second_line)
    print(f'  Will remove L{second_line} for {route}')

# Now find the function body for each marked line
# Functions typically start with `def ` after the decorator
# We need to find the end of each function (next decorator or end of file)

# Build a set of line numbers to remove (including function bodies)
remove_ranges = []
for second_line in sorted(lines_to_remove):
    # Find the function name
    func_line = second_line  # line after @app.route
    while func_line <= len(lines):
        stripped = lines[func_line - 1].strip()
        if stripped.startswith('def '):
            func_name = stripped.split('(')[0].replace('def ', '')
            break
        func_line += 1
    
    # Find end of function (next @app.route or end of file, indented less)
    start_idx = second_line - 1  # 0-indexed
    end_idx = start_idx
    for j in range(start_idx, len(lines)):
        line = lines[j]
        # If we hit another decorator or a top-level def, stop
        if j > start_idx and (line.strip().startswith('@app.route') or 
                               (line.strip().startswith('def ') and not line.startswith('    '))):
            break
        end_idx = j
    
    remove_ranges.append((start_idx, end_idx, second_line, func_name if 'func_name' in dir() else '?'))
    print(f'  Function body: L{second_line+1}-L{end_idx+1}')

# Remove ranges (in reverse order to preserve line numbers)
remove_ranges.sort(key=lambda x: x[0], reverse=True)
for start, end, orig_line, func_name in remove_ranges:
    print(f'  Removing L{start+1}-L{end+1} ({func_name})')
    del lines[start:end+1]

# Write back
with open(path, 'w', encoding='utf-8') as f:
    f.writelines(lines)

print(f'\nDone! Removed {len(remove_ranges)} duplicate functions.')
print(f'New file has {len(lines)} lines (was {len(lines) + sum(e-s+1 for s,e,_,_ in remove_ranges)})')
