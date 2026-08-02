// Popup controller da extensão do PJe

let processInfo = null;

document.addEventListener("DOMContentLoaded", async () => {
  const cnjVal = document.getElementById("cnj-val");
  const pecasVal = document.getElementById("pecas-val");
  const analisarBtn = document.getElementById("analisar-btn");
  const logBox = document.getElementById("log");
  const endpointInput = document.getElementById("endpoint");
  const tokenInput = document.getElementById("token");

  const config = await chrome.storage.local.get(["endpoint", "extensionToken"]);
  endpointInput.value = config.endpoint || "http://127.0.0.1:8001";
  tokenInput.value = config.extensionToken || "";

  function addLog(text, type = "info") {
    logBox.style.display = "block";
    const entry = document.createElement("div");
    entry.className = `log-entry log-${type}`;
    entry.innerText = `> ${text}`;
    logBox.appendChild(entry);
    logBox.scrollTop = logBox.scrollHeight;
  }

  // Consulta a aba ativa para obter os dados do processo
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) {
    cnjVal.innerText = "Nenhuma aba ativa";
    return;
  }

  try {
    chrome.tabs.sendMessage(tab.id, { action: "extrairDadosProcesso" }, (response) => {
      if (chrome.runtime.lastError || !response || !response.cnj) {
        cnjVal.innerText = "Abra os autos do PJe";
        pecasVal.innerText = "Nenhum processo detectado";
        return;
      }

      processInfo = response;
      cnjVal.innerText = response.cnj;
      pecasVal.innerText = `${response.documentos.length} peças encontradas`;
      analisarBtn.removeAttribute("disabled");
      addLog("Autos detectados com sucesso!");
    });
  } catch (err) {
    cnjVal.innerText = "Erro ao injetar script";
  }

  analisarBtn.addEventListener("click", async () => {
    if (!processInfo) return;
    analisarBtn.setAttribute("disabled", "true");
    logBox.innerHTML = "";
    
    addLog(`Iniciando extração do processo ${processInfo.cnj}...`, "info");
    
    // Obter URL base do PJe a partir da aba ativa
    const urlBase = new URL(processInfo.url).origin;
    const documentosComTexto = [];
    
    // Baixa o texto das peças utilizando o navegador (autenticado via cookies locais)
    const limit = Math.min(processInfo.documentos.length, 30); // Limita a 30 peças para performance
    addLog(`Carregando texto de ${limit} peças...`, "info");
    
    for (let i = 0; i < limit; i++) {
      const doc = processInfo.documentos[i];
      addLog(`Lendo [${i + 1}/${limit}] ID: ${doc.id} - ${doc.titulo.substring(0, 20)}...`, "info");
      
      try {
        // Tenta buscar no visualizador HTML nativo do PJe
        const targetUrl = `${urlBase}/pje/Processo/ConsultaProcesso/Detalhe/documentoHTML.seam?idBinario=${doc.id}`;
        const res = await fetch(targetUrl);
        if (res.ok) {
          const html = await res.text();
          // Remove tags HTML de forma simples para extrair o texto limpo
          const tempDiv = document.createElement("div");
          tempDiv.innerHTML = html;
          const textoLimpo = tempDiv.innerText || tempDiv.textContent || "";
          
          doc.texto = textoLimpo.replace(/\s+/g, " ").trim();
          documentosComTexto.push(doc);
        } else {
          doc.texto = "[Sem conteúdo legível ou PDF complexo]";
          documentosComTexto.push(doc);
        }
      } catch (err) {
        addLog(`Erro ao ler documento ${doc.id}: ${err.message}`, "error");
        doc.texto = "[Erro na leitura local]";
        documentosComTexto.push(doc);
      }
    }
    
    addLog("Enviando dados consolidados ao Servidor MCP local...", "info");
    
    const endpoint = endpointInput.value.trim().replace(/\/$/, "");
    const token = tokenInput.value.trim();
    let parsedEndpoint;
    try {
      parsedEndpoint = new URL(endpoint);
      if (!["127.0.0.1", "localhost"].includes(parsedEndpoint.hostname)) {
        throw new Error("somente servidores locais são aceitos");
      }
      if (parsedEndpoint.protocol !== "http:") {
        throw new Error("o servidor local deve usar HTTP");
      }
    } catch (err) {
      addLog(`Configuração inválida: ${err.message}`, "error");
      analisarBtn.removeAttribute("disabled");
      return;
    }
    await chrome.storage.local.set({ endpoint, extensionToken: token });

    // Envia o payload consolidado para o endpoint HTTP local do MCP
    try {
      const headers = { "Content-Type": "application/json" };
      if (token) headers["X-Extension-Token"] = token;
      const mcpRes = await fetch(`${endpoint}/analise_extensao`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          numero_cnj: processInfo.cnj,
          grau: "1g",
          persona: "servidor",
          base: {
            process_number: processInfo.cnj,
            grau: "1g"
          },
          documents: documentosComTexto,
          expedientes: []
        })
      });
      
      if (mcpRes.ok) {
        const result = await mcpRes.json();
        addLog("🎉 Análise concluída e integrada com sucesso!", "success");
        addLog(`Dossiê gravado no SQLite sob o ID do Job correspondente.`, "success");
        
        // Abre o dossiê detalhado no browser
        const blob = new Blob([JSON.stringify(result.dossier, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        window.open(url, "_blank");
      } else {
        const errorText = await mcpRes.text();
        addLog(`Falha na API do MCP: ${errorText}`, "error");
      }
    } catch (err) {
      addLog(`Erro de conexão com o MCP local: ${err.message}.`, "error");
    }
    
    analisarBtn.removeAttribute("disabled");
  });
});
