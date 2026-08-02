// Script de Conteúdo do PJe Assistente

// Função para tentar extrair o CNJ do processo da página ativa
function extrairCNJ() {
  const padraoCNJ = /\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/;
  
  // 1. Tenta no título da página
  let match = document.title.match(padraoCNJ);
  if (match) return match[0];
  
  // 2. Tenta em elementos de cabeçalho comuns do PJe
  const seletores = [
    ".numero-processo", 
    ".processo-cabecalho",
    "#numeroProcesso",
    ".numProcesso",
    "span[id*='numeroProcesso']",
    "div[id*='cabecalho']"
  ];
  for (let seletor of seletores) {
    const el = document.querySelector(seletor);
    if (el && el.innerText) {
      match = el.innerText.match(padraoCNJ);
      if (match) return match[0];
    }
  }
  
  // 3. Tenta varrer todo o body
  match = document.body.innerText.match(padraoCNJ);
  if (match) return match[0];
  
  return null;
}

// Função para raspar a árvore lateral de documentos
function rasparArvoreDocumentos() {
  const documentos = [];
  
  // O PJe geralmente usa uma estrutura de lista para a árvore de documentos (ex: tags <a> ou <div>)
  // que acionam a visualização da peça. Nós buscamos os IDs de download ou visualização.
  // Muitas vezes chamam funções como abrirLinkDocumento('12345') ou baixarDocumento('12345')
  const links = document.querySelectorAll("a, div[onclick], span[onclick]");
  
  links.forEach(el => {
    const onclick = el.getAttribute("onclick") || "";
    const href = el.getAttribute("href") || "";
    const texto = el.innerText || "";
    
    // Tenta capturar IDs de documentos via funções de clique comuns
    const matchId = (onclick + href).match(/(\d{8,10})/);
    if (matchId) {
      const docId = matchId[1];
      
      // Evita duplicados
      if (!documentos.some(d => d.id === docId)) {
        // Tenta inferir o tipo a partir do texto vizinho ou do próprio link
        let tipo = "Outro";
        if (texto.includes("Sentença") || texto.includes("sentenca")) tipo = "Sentença";
        else if (texto.includes("Petição") || texto.includes("peticao")) tipo = "Petição";
        else if (texto.includes("Decisão") || texto.includes("decisao")) tipo = "Decisão";
        else if (texto.includes("Certidão") || texto.includes("certidao")) tipo = "Certidão";
        else if (texto.includes("Ofício") || texto.includes("oficio")) tipo = "Ofício";
        
        documentos.push({
          id: docId,
          tipo: tipo,
          titulo: texto.trim() || `${tipo} (${docId})`,
          texto: "" // Será preenchido via fetch
        });
      }
    }
  });
  
  return documentos;
}

// Escuta mensagens vindas do Popup da extensão
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "extrairDadosProcesso") {
    const cnj = extrairCNJ();
    const documentos = rasparArvoreDocumentos();
    
    sendResponse({
      cnj: cnj,
      documentos: documentos,
      url: window.location.href
    });
  }
  return true;
});
