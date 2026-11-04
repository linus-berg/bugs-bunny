"""PDF text extraction service."""

import os

__version__ = "1.0.0"

# The onnxruntime wheel (used by the layout model) ships Microsoft's 1DS
# telemetry client, which is enabled by default. A service that handles
# other people's documents should not phone home, so opt out unless the
# operator explicitly says otherwise. This runs before onnxruntime is
# imported, in the web process and in every worker.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
