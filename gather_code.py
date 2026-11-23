import os

EXCLUDE_DIRS = {'.git', '__pycache__', 'venv', '.venv', 'env', 'node_modules', '.env'}
EXCLUDE_FILES = {'gather_code.py', 'package-lock.json', '.DS_Store', 'project_context.txt'}
VALID_EXTENSIONS = {'.py', '.js', '.jsx', '.html', '.css', '.ts', '.json'}

output_file = "project_context.txt"

with open(output_file, "w", encoding="utf-8") as out:
    out.write("# СТРУКТУРА И КОД МОЕГО ПРОЕКТА\n\n")
    out.write("## 1. Дерево файлов:\n```text\n")
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        level = root.replace('.', '').count(os.sep)
        indent = ' ' * 4 * (level)
        out.write(f"{indent}{os.path.basename(root)}/\n")
        subindent = ' ' * 4 * (level + 1)
        for f in files:
            if f not in EXCLUDE_FILES and os.path.splitext(f)[1] in VALID_EXTENSIONS:
                out.write(f"{subindent}{f}\n")
    out.write("```\n\n## 2. Исходный код файлов:\n\n")

    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for file in files:
            if file in EXCLUDE_FILES:
                continue
            ext = os.path.splitext(file)[1]
            if ext in VALID_EXTENSIONS:
                relative_path = os.path.join(root, file)
                out.write(f"### Файл: {relative_path}\n")
                out.write(f"```{ext[1:]}\n")
                try:
                    with open(relative_path, "r", encoding="utf-8") as f:
                        out.write(f.read())
                except Exception as e:
                    out.write(f"// Не удалось прочитать файл: {e}")
                out.write("\n```\n\n")

print(f"Готово! Весь контекст сохранен в файл {output_file}")
