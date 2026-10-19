import ast

def extract_function(filename, func_names):
    with open(filename, 'r', encoding='utf-8') as f:
        source = f.read()
    
    module = ast.parse(source)
    lines = source.split('\n')
    extracted = ""
    
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in func_names:
            start_lineno = node.lineno - 1
            end_lineno = node.end_lineno
            extracted += '\n'.join(lines[start_lineno:end_lineno]) + '\n\n'
            
    return extracted

funcs = ['monitor_virtual_orders']
content = extract_function('E:/Level 3 Term 2/Ai/Tradingbot/main.py', funcs)
with open('extracted_funcs3.txt', 'w', encoding='utf-8') as f:
    f.write(content)
print("Extracted successfully.")
