# Política de segurança

## Invariantes

- O produto não assina, protocola nem movimenta processos no PJe.
- Credenciais e chaves vêm de mecanismos externos; não existem fallbacks.
- Dados processuais e pessoais não podem ser versionados, nem como fixture.
- Testes padrão são offline e não autenticam no PJe.
- Bancos, downloads, exports e logs vivem em armazenamento externo controlado.

## Antes de commitar

Execute `python3 scripts/check_repository_hygiene.py`. A CI repete essa
verificação e bloqueia chaves, caminhos pessoais e identificadores processuais
fora das áreas sintéticas permitidas.

Credenciais encontradas no histórico devem ser tratadas como comprometidas.
Rotação acontece antes da publicação de qualquer histórico reescrito.
