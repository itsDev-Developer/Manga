import os
import zipfile

from loguru import logger


def images_to_cbz(image_files, cbz_output_path):
  """Images are already JPEG, so store them (no CPU wasted re-compressing)."""
  try:
    with zipfile.ZipFile(cbz_output_path, "w", zipfile.ZIP_STORED) as zip_file:
      for image_file_path in image_files:
        zip_file.write(image_file_path, arcname=os.path.basename(image_file_path))
    logger.info(f"CBZ created at {cbz_output_path}")
  except Exception as e:
    logger.exception(f"Making CBZ: {e}")
    return e
