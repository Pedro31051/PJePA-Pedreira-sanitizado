# Extensão companion PJePA

A extensão extrai, no navegador autenticado do operador, dados visíveis dos
autos e os envia somente ao endpoint local `/analise_extensao` do servidor
PJePA-Pedreira. Ela não contém servidor próprio e não protocola nem movimenta
processos.

## Configuração

1. Carregue esta pasta como extensão não compactada no Chrome.
2. No popup, confirme o endpoint local. O padrão é
   `http://127.0.0.1:8001`.
3. Se o serviço definir `PJE_EXTENSION_TOKEN`, informe o mesmo token no popup.
   Ele é armazenado em `chrome.storage.local`, nunca no repositório.

O manifesto limita o endpoint a loopback e registra `content.js` apenas para
hosts do TJPA. A extensão limita a leitura a 30 peças por execução.
