"use client";

import { A2UISurfaceView } from "@/components/A2UISurfaceView";
import { SPECIFICATION_SURFACE_ID } from "@/lib/a2ui-surface-ids";

export function SpecificationView() {
  return (
    <div className="mx-auto max-w-3xl p-6">
      <A2UISurfaceView
        surfaceId={SPECIFICATION_SURFACE_ID}
        fallback={<p className="text-sm text-neutral-500">No Specification draft yet.</p>}
      />
    </div>
  );
}
