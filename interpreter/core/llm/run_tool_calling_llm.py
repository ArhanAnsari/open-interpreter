from .utils.merge_deltas import merge_deltas
from .utils.parse_partial_json import parse_partial_json

tool_schema = {
    "type": "function",
    "function": {
        "name": "execute",
        "description": "Executes code on the user's machine **in the users local environment** and returns the output",
        "parameters": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "The programming language (required parameter to the `execute` function)",
                    "enum": [
                        # This will be filled dynamically with the languages OI has access to.
                    ],
                },
                "code": {
                    "type": "string",
                    "description": "The code to execute (required)",
                },
            },
            "required": ["language", "code"],
        },
    },
}


def run_tool_calling_llm(llm, request_params):
    ## Setup

    # Add languages OI has access to
    tool_schema["function"]["parameters"]["properties"]["language"]["enum"] = [
        i.name.lower() for i in llm.interpreter.computer.terminal.languages
    ]
    request_params["tools"] = [tool_schema]

    last_tool_id = 0
    for i, message in enumerate(request_params["messages"]):
        if "function_call" in message:
            last_tool_id += 1
            function = message.pop("function_call")
            message["tool_calls"] = [
                {
                    "id": "toolu_" + str(last_tool_id),
                    "type": "function",
                    "function": function,
                }
            ]
        if message["role"] == "function":
            if i != 0 and request_params["messages"][i - 1]["role"] == "tool":
                request_params["messages"][i]["content"] += message["content"]
                message = None
            else:
                message["role"] = "tool"
                message["tool_call_id"] = "toolu_" + str(last_tool_id)

    request_params["messages"] = [m for m in request_params["messages"] if m != None]

    # Add OpenAI's recommended function message
    # request_params["messages"][0][
    #     "content"
    # ] += "\nUse ONLY the function you have been provided with — 'execute(language, code)'."

    ## Convert output to LMC format

    accumulated_deltas = {}
    language = None
    code = ""
    function_call_detected = False
    accumulated_review = ""
    review_category = None

    for chunk in llm.completions(**request_params):
        if "choices" not in chunk or len(chunk["choices"]) == 0:
            # This happens sometimes
            continue

        delta = chunk["choices"][0]["delta"]

        # Convert tool call into function call, which we have great parsing logic for below
        if "tool_calls" in delta and delta["tool_calls"]:
            function_call_detected = True

            # import pdb; pdb.set_trace()
            if len(delta["tool_calls"]) > 0 and delta["tool_calls"][0].function:
                delta = {
                    # "id": delta["tool_calls"][0],
                    "function_call": {
                        "name": delta["tool_calls"][0].function.name,
                        "arguments": delta["tool_calls"][0].function.arguments,
                    }
                }

        # Accumulate deltas
        accumulated_deltas = merge_deltas(accumulated_deltas, delta)

        if "content" in delta and delta["content"]:
            if function_call_detected:
                # More content after a code block? This is a code review by a judge layer.

                # print("Code safety review:", delta["content"])

                if review_category == None:
                    accumulated_review += delta["content"]

                    if "<unsafe>" in accumulated_review:
                        review_category = "unsafe"
                    if "<warning>" in accumulated_review:
                        review_category = "warning"
                    if "<safe>" in accumulated_review:
                        review_category = "safe"

                if review_category != None:
                    for tag in [
                        "<safe>",
                        "</safe>",
                        "<warning>",
                        "</warning>",
                        "<unsafe>",
                        "</unsafe>",
                    ]:
                        delta["content"] = delta["content"].replace(tag, "")

                    yield {
                        "type": "review",
                        "format": review_category,
                        "content": delta["content"],
                    }

            else:
                yield {"type": "message", "content": delta["content"]}

        if (
            accumulated_deltas.get("function_call")
            and "arguments" in accumulated_deltas["function_call"]
            and accumulated_deltas["function_call"]["arguments"]
        ):
            if (
                "name" in accumulated_deltas["function_call"]
                and accumulated_deltas["function_call"]["name"] == "execute"
            ):
                arguments = accumulated_deltas["function_call"]["arguments"]
                arguments = parse_partial_json(arguments)

                if arguments:
                    if (
                        language is None
                        and "language" in arguments
                        and "code"
                        in arguments  # <- This ensures we're *finished* typing language, as opposed to partially done
                        and arguments["language"]
                    ):
                        language = arguments["language"]

                    if language is not None and "code" in arguments:
                        # Calculate the delta (new characters only)
                        code_delta = arguments["code"][len(code) :]
                        # Update the code
                        code = arguments["code"]
                        # Yield the delta
                        if code_delta:
                            yield {
                                "type": "code",
                                "format": language,
                                "content": code_delta,
                            }
                else:
                    if llm.interpreter.verbose:
                        print("Arguments not a dict.")

            # Common hallucinations
            elif "name" in accumulated_deltas["function_call"] and (
                accumulated_deltas["function_call"]["name"] == "python"
                or accumulated_deltas["function_call"]["name"] == "functions"
            ):
                if llm.interpreter.verbose:
                    print("Got direct python call")
                if language is None:
                    language = "python"

                if language is not None:
                    # Pull the code string straight out of the "arguments" string
                    code_delta = accumulated_deltas["function_call"]["arguments"][
                        len(code) :
                    ]
                    # Update the code
                    code = accumulated_deltas["function_call"]["arguments"]
                    # Yield the delta
                    if code_delta:
                        yield {
                            "type": "code",
                            "format": language,
                            "content": code_delta,
                        }

            else:
                # If name exists and it's not "execute" or "python" or "functions", who knows what's going on.
                if "name" in accumulated_deltas["function_call"]:
                    yield {
                        "type": "code",
                        "format": "python",
                        "content": accumulated_deltas["function_call"]["name"],
                    }
                    return
