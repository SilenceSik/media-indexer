import time

from core.image_cache import ImageCache


class MetadataManager:

    def __init__(
        self,
        client,
        database
    ):

        self.client = client

        self.database = database

        self.image_cache = ImageCache(
            "storage/images/covers"
        )

    def update(
        self,
        number
    ):

        print(
            "查询:",
            number
        )

        data = self.client.query(
            number
        )

        if not data:

            return False

        cover = data.get(
            "cover"
        )

        local_cover = self.image_cache.download(
            cover
        )

        data["cover_local"] = local_cover

        data["updated_time"] = time.time()

        self.database.save_metadata(
            data
        )

        return True
