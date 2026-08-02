# Convenção de nomenclatura

- Domínio jurídico e interface pública: português, `snake_case`.
- Infraestrutura reutilizável e contratos técnicos: inglês, `snake_case`.
- Classes: `PascalCase`; constantes: `UPPER_SNAKE_CASE`.
- Testes offline: `test_*.py`; scripts reais ficam em `scripts/live`.
- Documentos históricos incluem data no nome e aviso de documento histórico.

A convenção é prospectiva. Módulos públicos existentes não devem ser renomeados
apenas por estética; mudanças exigem fachada compatível e teste de importação.
