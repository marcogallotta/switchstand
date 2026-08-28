"use strict";

(function installNativeSelectionController(root) {
  const STORAGE_KEY = "switchstand.native-selection.v1";
  const PAIR_FIELDS = ["agentRef", "observationRunRef"];

  function exactPair(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    if (Object.keys(value).sort().join("|") !== PAIR_FIELDS.join("|")) return null;
    if (typeof value.observationRunRef !== "string" || !value.observationRunRef) return null;
    if (typeof value.agentRef !== "string" || !value.agentRef) return null;
    return { observationRunRef: value.observationRunRef, agentRef: value.agentRef };
  }

  function samePair(left, right) {
    return left?.observationRunRef === right?.observationRunRef
      && left?.agentRef === right?.agentRef;
  }

  function safeStorage(storage, method, ...arguments_) {
    try {
      return storage?.[method](...arguments_);
    } catch (_error) {
      return undefined;
    }
  }

  function restoredPair(storage) {
    const encoded = safeStorage(storage, "getItem", STORAGE_KEY);
    if (encoded === null || encoded === undefined) return null;
    try {
      const pair = exactPair(JSON.parse(encoded));
      if (pair) return pair;
    } catch (_error) {
      // Invalid browser state is cleared below.
    }
    safeStorage(storage, "removeItem", STORAGE_KEY);
    return null;
  }

  function successfulTarget(result, pair) {
    if (!result || result.version !== "native-selection-v1" || !samePair(result, pair)
      || result.connected !== true || result.present !== true) return null;
    const target = { version: "native-selection-v1", ...pair, connected: true, present: true };
    if (typeof result.name === "string") target.name = result.name;
    if (typeof result.agentNickname === "string") target.agentNickname = result.agentNickname;
    return target;
  }

  function seamParts(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).sort().join("|") !== "selection|snapshot") return null;
    const selection = exactPair(value.selection);
    return selection ? { selection, snapshot: value.snapshot } : null;
  }

  function createController({ resolve, storage, onChange = () => {} }) {
    if (typeof resolve !== "function") throw new TypeError("resolve must be a function");
    let candidate = restoredPair(storage);
    let currentTarget = null;
    let suppliedSnapshot;
    let snapshotGeneration = 0;
    let requestGeneration = 0;

    const state = () => ({
      candidate: candidate ? { ...candidate } : null,
      currentTarget: currentTarget ? { ...currentTarget } : null,
    });
    const emit = () => onChange(state());
    const removeStoredPair = () => safeStorage(storage, "removeItem", STORAGE_KEY);

    function clear() {
      requestGeneration += 1;
      candidate = null;
      currentTarget = null;
      removeStoredPair();
      emit();
    }

    async function revalidate() {
      if (!candidate || suppliedSnapshot === undefined) return;
      const pair = { ...candidate };
      const expectedSnapshotGeneration = snapshotGeneration;
      const expectedRequestGeneration = ++requestGeneration;
      let result;
      try {
        result = await resolve(pair, suppliedSnapshot);
      } catch (_error) {
        result = null;
      }
      if (expectedRequestGeneration !== requestGeneration
        || expectedSnapshotGeneration !== snapshotGeneration || !samePair(pair, candidate)) return;
      const target = successfulTarget(result, pair);
      if (!target) {
        clear();
        return;
      }
      currentTarget = target;
      safeStorage(storage, "setItem", STORAGE_KEY, JSON.stringify(pair));
      emit();
    }

    function select(value) {
      const seam = seamParts(value);
      if (!seam) {
        clear();
        return Promise.resolve();
      }
      requestGeneration += 1;
      candidate = seam.selection;
      suppliedSnapshot = seam.snapshot;
      snapshotGeneration += 1;
      currentTarget = null;
      removeStoredPair();
      emit();
      return revalidate();
    }

    function supplySeam(value) {
      const seam = seamParts(value);
      if (!seam) {
        clear();
        return Promise.resolve();
      }
      suppliedSnapshot = seam.snapshot;
      snapshotGeneration += 1;
      requestGeneration += 1;
      return candidate ? revalidate() : Promise.resolve();
    }

    function invalidate(value) {
      const seam = seamParts(value);
      if (candidate && seam) {
        suppliedSnapshot = seam.snapshot;
        snapshotGeneration += 1;
        requestGeneration += 1;
        void revalidate();
      }
      clear();
    }

    return { clear, getState: state, invalidate, select, supplySeam };
  }

  root.SwitchstandNativeSelection = { createController, storageKey: STORAGE_KEY };
}(globalThis));
