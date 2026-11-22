"""The code the three Ray packages (ray_jobs, ray_core, ray_serving) share: the staging store, the job spec envelope check, the report
envelope and status words, the layout keys and vocabularies mirrored from core-api, and the image/tabular preprocessing math that
the trainer and the serving replica must apply identically.

It is one source (ray-common/ray_common) shipped inside each package's zip, as a sibling of the package directory
(`package/ray_common/` next to `package/ray_jobs/`), so each zip stays self-contained and `import ray_common` works wherever the
package imports. It imports nothing heavy at module level: numpy and Pillow live in `ray_common.preprocess`, which only the
trainer and the serving encoders import; boto3 is imported when an S3Store is built. It imports no sibling package.
"""
