# Marker so setuptools can discover the package and the gbrain-doctor
# entry point resolves after `pip install .`. The non-Python files in
# scripts/ (.sh helpers) are still installed as data via MANIFEST.in
# when listed; otherwise they remain repo-only assets.
