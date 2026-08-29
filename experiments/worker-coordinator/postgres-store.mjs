const CALLS = Object.freeze({
  admit: ['SELECT coordinator_v2.admit_work_v2($1::jsonb) AS value', 1],
  claim: ['SELECT coordinator_v2.claim_v2($1::uuid,$2::uuid) AS value', 2],
  renew: ['SELECT coordinator_v2.renew_v2($1::jsonb) AS value', 1],
  checkout: ['SELECT coordinator_v2.checkout_authority_v2($1::jsonb) AS value', 1],
  checkpoint: ["SELECT coordinator_v2.mutate_worker_v2('checkpoint',$1::jsonb) AS value", 1],
  candidate: ["SELECT coordinator_v2.mutate_worker_v2('submit_candidate',$1::jsonb) AS value", 1],
  complete: ["SELECT coordinator_v2.mutate_worker_v2('complete',$1::jsonb) AS value", 1],
  cancel: ['SELECT coordinator_v2.cancel_v2($1::text) AS value', 1],
  authorize: ['SELECT coordinator_v2.authorize_publication_v2($1::text) AS value', 1],
  claimPublication: ['SELECT coordinator_v2.claim_publication_v2($1::uuid) AS value', 1],
  recordObjects: [
    'SELECT coordinator_v2.record_objects_v2($1::uuid,$2::bigint,$3::uuid,$4::text,$5::text,$6::text) AS value',
    6,
  ],
  failPublication: [
    'SELECT coordinator_v2.fail_publication_v2($1::uuid,$2::bigint,$3::uuid,$4::text) AS value',
    4,
  ],
  observePublication: [
    'SELECT coordinator_v2.observe_publication_v2(' +
      '$1::uuid,$2::bigint,$3::uuid,$4::text,$5::text,$6::text,$7::boolean) AS value',
    7,
  ],
  exportWork: ['SELECT coordinator_v2.export_work_v2($1::text,$2::boolean) AS value', 2],
});

const RETRYABLE = new Set(['40001', '40P01']);

function parameters(values) {
  return values.map((value) =>
    value !== null && typeof value === 'object' ? JSON.stringify(value) : value,
  );
}

export class PostgresStore {
  constructor(database) {
    this.database = database;
  }

  async call(name, values) {
    const operation = CALLS[name];
    if (!operation || values.length !== operation[1]) {
      throw Object.assign(new Error('invalid_store_operation'), { code: 'invalid_request' });
    }
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        return await this.database.transaction(async (transaction) => {
          await transaction.exec('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE');
          const result = await transaction.query(operation[0], parameters(values));
          return result.rows[0]?.value ?? null;
        });
      } catch (error) {
        if (RETRYABLE.has(error?.code) && attempt < 2) continue;
        const safeCodes = [
          'unauthorized', 'invalid_request', 'policy_denied', 'idempotency_conflict',
          'not_found', 'stale_or_invalid_lease', 'terminal_immutable', 'stale_head',
          'publication_already_authorized', 'request_too_large', 'determinism_violation',
          'invalid_admission_binding',
        ];
        const fixed = safeCodes.find((code) => String(error?.message || '').includes(code));
        if (fixed) error.fixedCode = fixed;
        throw error;
      }
    }
    throw new Error('temporary_failure');
  }
}
