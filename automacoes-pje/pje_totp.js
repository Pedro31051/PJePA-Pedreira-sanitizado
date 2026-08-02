/**
 * pje_totp.js — Implementação pura do algoritmo TOTP para validação offline
 * contra os vetores públicos do RFC 6238.
 *
 * TOTP RFC 6238 (HMAC-SHA1, janela de 30s, 6 dígitos) implementado em Node puro
 * — sem dependências externas. Por regra local, sementes reais, leitura de
 * Keychain, geração do código vigente e preenchimento do SSO não pertencem a
 * este módulo: o segundo fator é sempre informado manualmente pelo Pedro.
 */
const crypto = require('crypto');

// Base32 (RFC 4648, sem padding) -> Buffer de bytes.
function base32ToBuffer(b32) {
  const alfabeto = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  const limpo = String(b32).toUpperCase().replace(/[^A-Z2-7]/g, '');
  let bits = '';
  for (const c of limpo) {
    const idx = alfabeto.indexOf(c);
    if (idx === -1) throw new Error('caractere Base32 inválido: ' + c);
    bits += idx.toString(2).padStart(5, '0');
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

/**
 * Gera o código TOTP. `paraTimestampMs` permite reproduzir vetores de teste
 * conhecidos (RFC 6238); em produção fica null e usa o relógio atual.
 */
function gerarCodigo(seedBase32, { digitos = 6, periodo = 30, paraTimestampMs = null } = {}) {
  const chave = base32ToBuffer(seedBase32);
  const epochS = Math.floor((paraTimestampMs == null ? Date.now() : paraTimestampMs) / 1000);
  let contador = Math.floor(epochS / periodo);
  const buf = Buffer.alloc(8);
  for (let i = 7; i >= 0; i--) { buf[i] = contador & 0xff; contador = Math.floor(contador / 256); }
  const hmac = crypto.createHmac('sha1', chave).update(buf).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const bin = ((hmac[offset] & 0x7f) << 24) | (hmac[offset + 1] << 16) |
              (hmac[offset + 2] << 8) | (hmac[offset + 3]);
  return (bin % 10 ** digitos).toString().padStart(digitos, '0');
}

module.exports = { gerarCodigo, base32ToBuffer };

// Execução direta: node pje_totp.js — valida o gerador contra os vetores
  // oficiais do RFC 6238. Nenhum segredo real é lido ou exibido.
if (require.main === module) {
  // Vetor oficial RFC 6238 (Appendix B), modo SHA1: seed ASCII
  // "12345678901234567890" => Base32 "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ".
  const seedTeste = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ';
  const esperados = [
    [59, '94287082'],
    [1111111109, '07081804'],
    [1111111111, '14050471'],
    [1234567890, '89005924'],
    [2000000000, '69279037'],
  ];
  let ok = true;
  for (const [t, esp] of esperados) {
    const got = gerarCodigo(seedTeste, { digitos: 8, paraTimestampMs: t * 1000 });
    const passou = got === esp;
    ok = ok && passou;
    console.log(`${passou ? '✅' : '❌'} T=${String(t).padStart(11)}s  esperado=${esp}  obtido=${got}`);
  }
  console.log(ok
    ? '\n✅ Gerador TOTP validado contra os vetores oficiais do RFC 6238.'
    : '\n❌ FALHOU — a matemática do gerador está incorreta.');

  process.exit(ok ? 0 : 1);
}
