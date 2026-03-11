import os

# 想讓 AI 看到的檔案清單
target_files = ['main.py', 'requirements.txt'] # 你可以自己增加其他檔案
target_dirs = ['dashboard'] # 如果有資料夾，也會掃描裡面的內容

print("--- PROJECT SNAPSHOT START ---")
for root, dirs, files in os.walk("."):
    # 避開隱藏資料夾 (如 .git)
    dirs[:] = [d for d in dirs if not d.startswith('.')]
    
    for file in files:
        if file.endswith(('.py', '.html', '.css', '.js')) and file != 'snapshot.py':
            file_path = os.path.join(root, file)
            print(f"\n--- FILE: {file_path} ---")
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    print(f.read())
            except:
                print("[無法讀取檔案]")
print("\n--- PROJECT SNAPSHOT END ---")