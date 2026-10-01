import os
import re
from pathlib import Path

def rename_materials():
    """
    將 materials 資料夾中的檔案檔名前綴（如 1-1、2-3 等）移除
    例如：'1-1 AI 工程崛起.md' -> 'AI 工程崛起.md'
    """
    # 設定材料資料夾路徑
    materials_dir = Path('data/materials')

    # 檢查資料夾是否存在
    if not materials_dir.exists():
        print(f'錯誤：資料夾 {materials_dir} 不存在')
        return

    # 正則表達式：匹配開頭的數字-數字格式
    # 例如：1-1、10-2、1-1  等
    pattern = re.compile(r'^\d+-\d+\s+')

    renamed_count = 0

    # 遍歷所有檔案
    for file_path in materials_dir.iterdir():
        if file_path.is_file() and file_path.suffix == '.md':
            old_name = file_path.name

            # 檢查是否符合要刪除的格式
            if pattern.match(old_name):
                # 移除前綴
                new_name = pattern.sub('', old_name)
                new_path = file_path.parent / new_name

                # 檢查新檔名是否已存在
                if new_path.exists() and new_path != file_path:
                    print(f'警告：{new_name} 已存在，跳過 {old_name}')
                    continue

                # 重新命名
                try:
                    file_path.rename(new_path)
                    print(f'✓ {old_name} -> {new_name}')
                    renamed_count += 1
                except Exception as e:
                    print(f'✗ 重新命名失敗：{old_name} - {e}')
            else:
                print(f'跳過（不符格式）：{old_name}')

    print(f'\n完成！共重新命名 {renamed_count} 個檔案')

if __name__ == '__main__':
    # 切換到 backend 目錄
    os.chdir(Path(__file__).parent)
    rename_materials()
