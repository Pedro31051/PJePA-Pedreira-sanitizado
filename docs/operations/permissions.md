# Ownership e permissões

O serviço observado em 01/08/2026 executava como o proprietário da working
tree; por isso os diretórios `0700` não bloqueavam o processo. Nenhuma permissão
foi ampliada durante a reorganização.

A topologia canônica usa usuário e grupo dedicados `pjepa-mcp`:

- código/release: leitura e execução pelo serviço, sem escrita;
- `/var/lib/pjepa-mcp`: `0700`, escrita do serviço;
- `/var/log/pjepa-mcp`: `0700`, escrita do serviço;
- credenciais: `systemd LoadCredentialEncrypted`, nunca arquivos do repo;
- checkout de desenvolvimento: permissões definidas pelo proprietário, não
  compartilhadas automaticamente com produção.
