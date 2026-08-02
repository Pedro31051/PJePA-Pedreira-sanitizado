'use strict';

const MAX_MESSAGE_BYTES = 256 * 1024;
const COMMANDS = Object.freeze([
  'get_state',
  'search_process',
  'open_process',
  'list_documents',
  'navigate_document',
  'download_current_document',
  'open_expedients',
  'list_expedients',
  'inspect_document_join',
  'inspect_communications',
  'analyze_communication_plan',
  'inspect_document_issue',
  'analyze_document_issue_plan',
  'inspect_process_metadata',
  'preview_process_label_change',
  'apply_process_label_change',
  'inspect_retification',
  'list_retification_candidates',
  'preview_retification',
  'simulate_retification',
  'apply_retification',
  'extract_document',
  'consume_signal',
  'ack_signal',
  'dispatch_event',
]);

class PJeBridgeError extends Error {
  constructor(code, message, { retryable = false } = {}) {
    super(message);
    this.name = 'PJeBridgeError';
    this.code = code;
    this.retryable = retryable;
  }
}

function validateRequest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'A requisição deve ser um objeto JSON.');
  }
  if (typeof value.request_id !== 'string' ||
      !/^[A-Za-z0-9_-]{8,80}$/.test(value.request_id)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'request_id inválido.');
  }
  if (!COMMANDS.includes(value.command)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Comando não permitido.');
  }
  const payload = value.payload ?? {};
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'payload deve ser um objeto JSON.');
  }
  const timeoutMs = value.timeout_ms ?? 10_000;
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 30_000) {
    throw new PJeBridgeError('INVALID_REQUEST', 'timeout_ms deve estar entre 1000 e 30000.');
  }
  return {
    request_id: value.request_id,
    command: value.command,
    payload,
    timeout_ms: timeoutMs,
  };
}

function success(requestId, data) {
  return { request_id: requestId, ok: true, data, error: null };
}

function failure(requestId, error) {
  return {
    request_id: requestId || 'unknown',
    ok: false,
    data: null,
    error: {
      code: error.code || 'INTERNAL_ERROR',
      message: error.message || 'Falha interna no bridge PJe.',
      retryable: Boolean(error.retryable),
    },
  };
}

module.exports = {
  COMMANDS,
  MAX_MESSAGE_BYTES,
  PJeBridgeError,
  failure,
  success,
  validateRequest,
};
