import { copy } from "../copy/catalog.js";
import { openConfirm } from "../dialogs/confirm-dialog.js";

export class ActionController {
  constructor({ client, dialogs, announce = () => {}, refresh = async () => {} } = {}) {
    this.client = client;
    this.dialogs = dialogs;
    this.announce = typeof announce === "function" ? announce : () => {};
    this.refresh = typeof refresh === "function" ? refresh : async () => {};
    this.pending = new Map();
  }

  async execute(descriptor, {
    opener = document.activeElement,
    input = {},
    confirmed = false,
    idempotencyKey = "",
  } = {}) {
    const intent = String(descriptor?.action || descriptor?.intent || "");
    const objectKey = String(descriptor?.object_key || descriptor?.target_key || "");
    if (!intent || !objectKey || descriptor?.transportReady !== true) {
      throw new Error(
        descriptor?.disabledReason || copy("app.actions.module.message.3234b2449f"),
      );
    }
    const replayKey = idempotencyKey || crypto.randomUUID();
    if (descriptor.confirmation && !confirmed) {
      return openConfirm(this.dialogs, {
        opener,
        operation: descriptor.label || copy("app.actions.module.message.f8347f9a9d"),
        impact: descriptor.confirmation.impact,
        unchanged: descriptor.confirmation.unchanged,
        automatic: descriptor.confirmation.automatic,
        recovery: descriptor.confirmation.recovery,
        returnCheck: descriptor.confirmation.returnCheck,
        confirmLabel: descriptor.confirmation.confirmLabel,
        intent: { id: intent },
        idempotencyKey: replayKey,
        onConfirm: ({ idempotencyKey: confirmedKey }) => this.execute(descriptor, {
          opener,
          input,
          confirmed: true,
          idempotencyKey: confirmedKey,
        }),
      });
    }

    const operationKey = `${intent}:${objectKey}`;
    if (this.pending.has(operationKey)) return this.pending.get(operationKey);
    const request = this.client.post(
      "dashboard/intents",
      {
        intent,
        target_key: objectKey,
        expected_revision: descriptor.expected_revision,
        input: input && typeof input === "object" ? input : {},
      },
      {
        operation: descriptor.label || copy("app.actions.module.message.b59dfbb9a7"),
        idempotencyKey: replayKey,
      },
    ).then(async (payload) => {
      this.announce(payload?.message || copy("app.actions.module.message.9cea6b8861"));
      await this.refresh();
      return payload;
    });
    this.pending.set(operationKey, request);
    try {
      return await request;
    } finally {
      this.pending.delete(operationKey);
    }
  }

  dispose() {
    this.pending.clear();
  }
}
