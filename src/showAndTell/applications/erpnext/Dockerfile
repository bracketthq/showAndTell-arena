# One immutable image for the benchmark's complete Frappe site. HRMS requires
# ERPNext, so the canonical ERPNext fixture carries both applications rather
# than maintaining a second deployment of the same Frappe service topology.
ARG ERPNEXT_IMAGE=frappe/erpnext@sha256:a951e8c905161ec50246d5f6da8e324d89ffb8bb3b407b3508208890f6a9483c
FROM ${ERPNEXT_IMAGE}

ARG HRMS_REF=v16.7.1
ARG HRMS_COMMIT=fe9ad9d362406a1ec3f394e14f6b3d5684c79b83

USER frappe
WORKDIR /home/frappe/frappe-bench

# An app-scoped Frappe build rewrites assets.json with only that app's bundles.
# Preserve the pinned ERPNext manifest and merge HRMS into it so shared bundle
# URLs such as /login.bundle.css and /frappe-web.bundle.js still resolve.
RUN cp assets/assets.json /tmp/erpnext-assets.json \
    && bench set-config -g socketio_port 9000 \
    && bench get-app --branch "${HRMS_REF}" https://github.com/frappe/hrms.git \
    && test "$(git -C apps/hrms rev-parse HEAD)" = "${HRMS_COMMIT}" \
    && bench build --app hrms \
    && cp -a sites/assets/. assets/ \
    && python -c 'import json; from pathlib import Path; p = Path("assets/assets.json"); merged = json.loads(Path("/tmp/erpnext-assets.json").read_text()); merged.update(json.loads(p.read_text())); p.write_text(json.dumps(merged, indent=4) + "\n")' \
    && rm -rf apps/hrms/.git
