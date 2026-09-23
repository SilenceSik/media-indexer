import os


class FileAnalyzer:

    def analyze(
        self,
        filepath,
        number
    ):

        result = {

            "filepath":
            filepath,

            "number":
            number,

            "exists":
            os.path.exists(filepath),

            "filename":
            os.path.basename(filepath)

        }

        return result
