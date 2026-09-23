import hashlib


def calculate_hash(
    filepath,
    chunk_size=1024 * 1024
):

    sha256 = hashlib.sha256()

    with open(
        filepath,
        "rb"
    ) as f:

        while True:

            chunk = f.read(
                chunk_size
            )

            if not chunk:
                break

            sha256.update(
                chunk
            )

    return sha256.hexdigest()
