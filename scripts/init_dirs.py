from pathlib import Path

for d in ['seeds', 'src', 'src/search', 'src/llm']:
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    init_f = p / '__init__.py'
    if not init_f.exists() and d.startswith('src'):
        init_f.write_text('')
print("Directories initialized successfully.")
