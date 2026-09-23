class TemplateEngine:

    def render(
        self,
        template,
        data
    ):

        result = template

        for key, value in data.items():

            result = result.replace(
                "{" + key + "}",
                str(value)
            )

        return result
