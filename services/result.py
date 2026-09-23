import uuid


def success(
    data
):

    return {

        "success": True,

        "data": data,

        "error": None,

        "trace_id":
        str(uuid.uuid4())

    }


def failure(
    error
):

    return {

        "success": False,

        "data": None,

        "error": error,

        "trace_id":
        str(uuid.uuid4())

    }
