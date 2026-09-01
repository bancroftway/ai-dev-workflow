"use client";

import { useEffect, useId, useState } from "react";

let initialized = false;

/**
 * Renders Mermaid diagram source directly in the browser (user requirement 2026-09-01) --
 * self-contained like a wireframe's inline HTML, no fetch/proxy/push-state dependency. Replaces
 * an earlier version that fetched the backend's server-rendered SVG through the raw-content
 * proxy: that meant a diagram committed but not yet PUSHED 404'd (found live immediately after
 * that feature shipped), and more generally meant the reviewed diagram could lag one commit
 * behind the draft actually on screen. The backend's own mmdc-CLI render at verify time is
 * unaffected by this -- that is the deterministic syntax check (never LLM self-attestation);
 * this is purely the display path, using the same underlying mermaid engine so a source that
 * passed verify renders here too.
 *
 * `mermaid` is imported dynamically inside the effect, never at module scope, so it only ever
 * loads in the browser after mount -- it uses DOM APIs the server render pass doesn't have.
 */
export function MermaidDiagram({ source, name }: { source: string; name: string }) {
  const reactId = useId();
  const diagramId = `mermaid-${reactId.replace(/[^a-zA-Z0-9]/g, "")}`;
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function render() {
      try {
        const mermaid = (await import("mermaid")).default;
        if (!initialized) {
          // securityLevel: "strict" is load-bearing, not a default left in place: `source` is
          // LLM-drafted diagram text, and Mermaid's own renderer is what needs to be untrusted-
          // input-safe here, not this component. "strict" sanitizes labels/links (DOMPurify
          // internally) and disables script-capable constructs (`click` handlers, script tags in
          // labels) before the SVG below is ever produced -- Mermaid's own documented mitigation
          // for exactly this scenario, and why the resulting SVG is safe to inject directly.
          mermaid.initialize({ startOnLoad: false, theme: "default", securityLevel: "strict" });
          initialized = true;
        }
        const result = await mermaid.render(diagramId, source);
        if (!cancelled) setSvg(result.svg);
      } catch (err) {
        // Should be rare -- the backend's own mmdc render already validated this source at
        // verify time -- but never let a render hiccup take down the whole Plan view.
        if (!cancelled) setError(err instanceof Error ? err.message : "Diagram failed to render.");
      }
    }
    void render();
    return () => {
      cancelled = true;
    };
  }, [source, diagramId]);

  if (error) {
    return <p className="text-xs text-red-600">Could not render {name}: {error}</p>;
  }
  if (!svg) {
    return <p className="text-xs text-neutral-400">Rendering {name}…</p>;
  }
  return <div className="max-w-full [&_svg]:max-w-full" dangerouslySetInnerHTML={{ __html: svg }} />;
}
