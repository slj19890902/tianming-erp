// Same compatible non-secret operation-key algorithm used by the ERP main page.
(function (global) {
'use strict';

      var idempotencyKeyFallbackCounter = 0;
      function createIdempotencyKey() {
        var cryptoApi = typeof crypto !== "undefined" ? crypto : null;
        if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
          try {
            return cryptoApi.randomUUID();
          } catch (error) {
            // Continue to the compatible fallbacks.
          }
        }
        if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
          try {
            var bytes = new Uint8Array(16);
            cryptoApi.getRandomValues(bytes);
            bytes[6] = (bytes[6] & 15) | 64;
            bytes[8] = (bytes[8] & 63) | 128;
            var hex = "";
            for (var index = 0; index < bytes.length; index += 1) {
              var byteHex = bytes[index].toString(16);
              hex += byteHex.length === 1 ? "0" + byteHex : byteHex;
            }
            return (
              hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" +
              hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" +
              hex.slice(20)
            );
          } catch (error) {
            // Date/random fallback is sufficient for a non-secret idempotency key.
          }
        }
        idempotencyKeyFallbackCounter += 1;
        return (
          "idemp-" + Date.now().toString(36) + "-" +
          Math.random().toString(36).slice(2, 12) + "-" +
          idempotencyKeyFallbackCounter.toString(36)
        );
      }
      
global.TmOperationKey = { create: createIdempotencyKey };
})(typeof window === 'undefined' ? globalThis : window);
