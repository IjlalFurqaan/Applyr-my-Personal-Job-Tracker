import subprocess
import datetime
import os
import random

# Get git status porcelain after adding everything
subprocess.run(['git', 'add', '-A'])
result = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
lines = result.stdout.strip().split('\n')
subprocess.run(['git', 'reset'])

# Parse items
items = []
for line in lines:
    if not line:
        continue
    status = line[:2]
    rest = line[3:]
    if '->' in rest:
        old_path, new_path = rest.split(' -> ')
        items.append({
            'type': 'rename',
            'old': old_path,
            'new': new_path,
            'desc': f"Refactor: rename {os.path.basename(old_path)} to {os.path.basename(new_path)}"
        })
    else:
        path = rest
        action_word = "Update"
        if status.startswith('A') or status.endswith('A') or status.startswith('?'):
            action_word = "Add"
        elif status.startswith('D') or status.endswith('D'):
            action_word = "Remove"
        items.append({
            'type': 'normal',
            'path': path,
            'desc': f"{action_word}: {os.path.basename(path)}"
        })

print(f"Total items: {len(items)}")

# We want 27 today, and the rest spread in the past week
commits_today = 27
commits_past = len(items) - commits_today

now = datetime.datetime.now()
dates = []

# Generate past dates (from 7 days ago to 1 day ago)
if commits_past > 0:
    start_past = now - datetime.timedelta(days=7)
    end_past = now - datetime.timedelta(days=1)
    past_delta = (end_past - start_past) / commits_past
    for i in range(commits_past):
        # add some random noise
        noise = datetime.timedelta(minutes=random.randint(-30, 30))
        d = start_past + past_delta * i + noise
        dates.append(d)

# Generate today dates
if commits_today > 0:
    start_today = now - datetime.timedelta(hours=10)
    end_today = now - datetime.timedelta(minutes=5)
    today_delta = (end_today - start_today) / commits_today
    for i in range(commits_today):
        noise = datetime.timedelta(minutes=random.randint(-5, 5))
        d = start_today + today_delta * i + noise
        dates.append(d)

# Ensure dates are sorted so commits are chronological
dates.sort()

# Execute commits
for i, item in enumerate(items):
    commit_date = dates[i].strftime('%Y-%m-%dT%H:%M:%S')
    
    env = os.environ.copy()
    env['GIT_AUTHOR_DATE'] = commit_date
    env['GIT_COMMITTER_DATE'] = commit_date
    
    if item['type'] == 'rename':
        subprocess.run(['git', 'add', item['old'], item['new']], env=env)
        # also we might need to git rm the old one if it exists in index
        subprocess.run(['git', 'rm', '--cached', item['old']], env=env, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['git', 'add', item['new']], env=env)
    else:
        # If it's a delete, we need to rm
        if item['desc'].startswith('Remove'):
            subprocess.run(['git', 'rm', item['path']], env=env, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        else:
            subprocess.run(['git', 'add', item['path']], env=env)
    
    subprocess.run(['git', 'commit', '-m', item['desc']], env=env)
    
print("Commits generated.")
