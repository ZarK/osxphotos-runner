import datetime
import hashlib
import os
import re
import subprocess
import time

import imagehash
import osxphotos
import pyheif
from PIL import Image
from PIL import UnidentifiedImageError

# Constants
from config import NAS_NAME, NAS_USERNAME, NAS_PASSWORD
MOUNT_PATH = "/Volumes/Archive"
EXPORT_PATH = "/Volumes/Archive/Picture Sources 01 RAW/15. iCloud Photos"
NAS_URL = f"smb://{NAS_NAME}/Archive"
MOUNT_CMD = [
    "mount_smbfs",
    f"smb://{NAS_USERNAME}:{NAS_PASSWORD}@{NAS_NAME}/Archive",
    MOUNT_PATH
]

# Variables
OVERLAP_PERIOD = datetime.timedelta(days=35)  # 1 month overlap
TODAY = datetime.date.today()
START_DATE = TODAY - OVERLAP_PERIOD

# Global flags
VERBOSE = True
PAUSE_AFTER_EXPORT = False

# Report dictionary
report = {
    "originals_exported": 0,
    "edits_exported": 0,
    "live_photos_exported": 0,
    "duplicates_skipped": 0,
    "duplicates_created": 0
}
failed_files = []


def vprint(*args, **kwargs):
    """Print only if VERBOSE is True."""
    if VERBOSE:
        print(*args, **kwargs)


# 1. Regex to extract IMG_XXXX pattern
def extract_img_pattern(filename):
    # Check if the file is a JPG or HEIC
    if not filename.lower().endswith(('.jpg', '.heic')):
        return filename

    # Check if the filename already matches the IMG_1234 format
    if re.match(r'^IMG_\d{4}\.(jpg|heic)$', filename, re.IGNORECASE):
        return filename

    # Extract the IMG_XXXX pattern if it exists
    match = re.search(r'IMG_\d{4}', filename)
    if match:
        # Extract the file extension
        file_extension = os.path.splitext(filename)[1]
        return f"{match.group(0)}{file_extension}"
    else:
        return filename


# 2. Handle Live Photos
def get_live_photo_name(photo_name):
    base_name = os.path.splitext(photo_name)[0]
    return f"{base_name}_HEVC.MOV"


# 3. Ensure volume is mounted
def ensure_volume_mounted():
    if not os.path.ismount(MOUNT_PATH):
        # Ensure the mount point exists
        if not os.path.exists(MOUNT_PATH):
            os.makedirs(MOUNT_PATH)
        subprocess.run(MOUNT_CMD)
        # Wait for a few seconds to ensure mounting is complete
        time.sleep(5)


def set_file_timestamp(file_path, timestamp):
    """Set the modified date of the file to the given timestamp."""
    epoch_time = timestamp.timestamp()
    os.utime(file_path, (epoch_time, epoch_time))


def heic_to_pil(image_path):
    """Convert HEIC format to PIL Image."""
    heif_file = pyheif.read(image_path)
    image = Image.frombytes(
        heif_file.mode,
        heif_file.size,
        heif_file.data,
        "raw",
        heif_file.mode,
        heif_file.stride,
    )
    return image


def file_hash(filepath):
    """Compute the SHA-256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for block in iter(lambda: f.read(4096), b''):
            sha256.update(block)
    return sha256.hexdigest()


SUPPORTED_IMAGE_FORMATS = ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.gif', '.heic', '.avif']


def file_hashes_match(photo_path, existing_path):
    """
    Compare the perceptual hash of two images to determine if they are visually similar.
    """
    # Check if the file extensions are in the list of supported formats
    photo_ext = os.path.splitext(photo_path)[1].lower()
    existing_ext = os.path.splitext(existing_path)[1].lower()

    if photo_ext not in SUPPORTED_IMAGE_FORMATS or existing_ext not in SUPPORTED_IMAGE_FORMATS:
        # Fallback to file hash for unsupported formats
        return file_hash(photo_path) == file_hash(existing_path)

    try:
        if photo_ext == '.heic':
            photo_image = heic_to_pil(photo_path)
        else:
            photo_image = Image.open(photo_path)

        if existing_ext == '.heic':
            existing_image = heic_to_pil(existing_path)
        else:
            existing_image = Image.open(existing_path)

        photo_hash = imagehash.phash(photo_image)
        existing_hash = imagehash.phash(existing_image)

        return photo_hash == existing_hash

    except UnidentifiedImageError:
        # Fallback to file hash for non-image files or unsupported image formats
        return file_hash(photo_path) == file_hash(existing_path)

    except Exception as e:
        print(f"Error processing files {photo_path} or {existing_path}: {e}")
        return file_hash(photo_path) == file_hash(existing_path)


def export_photo_variant(photo, path, is_edited=False, is_live=False):
    """Helper function to export a photo variant."""
    # Check if file exists
    if os.path.exists(path):
        # Determine which photo path to use for hash comparison
        photo_path = photo.path_edited if is_edited else photo.path
        if not file_hashes_match(photo_path, path):
            vprint(f"Exporting {'edited ' if is_edited else ''}{'live ' if is_live else ''}photo to {path}")

            # Check for duplicates created by the system
            duplicate_path = os.path.join(os.path.dirname(path), os.path.basename(path).replace(".", " (1)."))

            # Check if a (1) duplicate already exists before the export
            pre_existing_duplicate = os.path.exists(duplicate_path)
            if not pre_existing_duplicate:
                photo.export(os.path.dirname(path), filename=os.path.basename(path), use_photos_export=is_edited,
                             live_photo=is_live)
            elif file_hashes_match(photo_path, duplicate_path):
                vprint(
                    f"Skipping duplicate {'edited ' if is_edited else ''}{'live ' if is_live else ''}photo: {duplicate_path}")
                report["duplicates_skipped"] += 1
                return True
            else:
                vprint(
                    f"Skipping due to hash mismatch with (1) duplicate for {'edited ' if is_edited else ''}{'live ' if is_live else ''}photo: {duplicate_path}")

            # Check if a (1) duplicate was created by the system during the export
            if os.path.exists(duplicate_path) and not pre_existing_duplicate:
                report["duplicates_created"] += 1
                path = duplicate_path
                vprint(
                    f"Duplicate created of {'edited ' if is_edited else ''}{'live ' if is_live else ''}photo to {path}")

            try:
                for _ in range(5):  # Try up to 5 times
                    if os.path.exists(path):
                        set_file_timestamp(path, photo.date)
                        break
                    time.sleep(1)  # Wait for 1 second before retrying
                else:
                    raise FileNotFoundError(f"{path} still does not exist after multiple checks.")
            except FileNotFoundError:
                print(f"Failed to set timestamp for {path}. File might not have been written yet.")
                failed_files.append({"path": path, "date": photo.date, "timestamp": photo.date.timestamp()})

            except Exception as e:
                print(f"Error exporting photo {photo.original_filename}: {e}")
                failed_files.append({"path": path, "date": photo.date, "timestamp": photo.date.timestamp()})

            # Update report
            if is_edited:
                report["edits_exported"] += 1
            elif is_live:
                report["live_photos_exported"] += 1
            else:
                report["originals_exported"] += 1

            if PAUSE_AFTER_EXPORT:
                input("Press Enter to continue to the next photo...")
            return False
        else:
            vprint(f"Skipping duplicate {'edited ' if is_edited else ''}{'live ' if is_live else ''}photo: {path}")
            report["duplicates_skipped"] += 1
            return True
    else:
        vprint(f"Exporting {'edited ' if is_edited else ''}{'live ' if is_live else ''}photo to {path}")
        photo.export(os.path.dirname(path), filename=os.path.basename(path), use_photos_export=is_edited,
                     live_photo=is_live)

        try:
            set_file_timestamp(path, photo.date)
        except FileNotFoundError:
            print(f"Failed to set timestamp for {path}. File might not have been written yet.")
            failed_files.append({"path": path, "date": photo.date, "timestamp": photo.date.timestamp()})

        except Exception as e:
            print(f"Error exporting photo {photo.original_filename}: {e}")
            failed_files.append({"path": path, "date": photo.date, "timestamp": photo.date.timestamp()})

        if PAUSE_AFTER_EXPORT:
            input("Press Enter to continue to the next photo...")

        # Update report
        if is_edited:
            report["edits_exported"] += 1
        elif is_live:
            report["live_photos_exported"] += 1
        else:
            report["originals_exported"] += 1
        return False


# 4. Export photos
def export_photos():
    photosdb = osxphotos.PhotosDB()
    total_photos = len([photo for photo in photosdb.photos() if photo.date.date() >= START_DATE])
    print(f"Found {total_photos} photos to process since {START_DATE}...")
    photos_to_process = [photo for photo in photosdb.photos() if photo.date.date() >= START_DATE]
    for idx, photo in enumerate(photos_to_process):
        # print(f"Processing photo {idx+1}/{total_photos} - {photo.original_filename}")
        year_month_path = os.path.join(EXPORT_PATH, str(photo.date.year), f"{photo.date.month:02}")
        os.makedirs(year_month_path, exist_ok=True)

        # Always export original
        original_filename = extract_img_pattern(photo.original_filename)
        original_path = os.path.join(year_month_path, original_filename)
        is_duplicate = export_photo_variant(photo, original_path)

        # If the original wasn't a duplicate, proceed with other variants
        if not is_duplicate:
            # Export edited version if exists
            if photo.hasadjustments:
                edited_filename_without_ext, file_extension = os.path.splitext(original_filename)
                edited_filename = f"{edited_filename_without_ext}-edited{file_extension}"
                edited_path = os.path.join(year_month_path, edited_filename)
                export_photo_variant(photo, edited_path, is_edited=True)

            # Handle live photos
            if photo.live_photo:
                live_photo_name = get_live_photo_name(original_filename)
                live_photo_path = os.path.join(year_month_path, live_photo_name)
                export_photo_variant(photo, live_photo_path, is_live=True)

    print("\n--- Export Report ---")
    print(f"Original photos exported: {report['originals_exported']}")
    print(f"Edited photos exported: {report['edits_exported']}")
    print(f"Live photos exported: {report['live_photos_exported']}")
    print(f"Duplicates skipped: {report['duplicates_skipped']}")
    print(f"Duplicates created: {report['duplicates_created']}")
    print("\nFiles that failed to set timestamp:")
    for file in failed_files:
        print(f"Path: {file['path']}, Date: {file['date']}, Timestamp: {file['timestamp']}")


# Main logic
if __name__ == "__main__":
    # ensure_volume_mounted()
    export_photos()
