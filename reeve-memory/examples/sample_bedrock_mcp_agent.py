import asyncio

import httpx
import ollama
from mcp import ClientSession
from mcp.client.sse import sse_client

# Your custom MCP Server endpoint
MCP_SERVER_URL = "http://mcp.reeve.co.in:8000/sse"

# Change this to whichever tool-capable model you are using locally
MODEL_NAME = "llama3.1"


async def main():
    print(f"🔌 Attempting to connect to MCP Server at {MCP_SERVER_URL}...")

    try:
        # 1. Connect to the MCP server via SSE
        async with sse_client(MCP_SERVER_URL) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                print("✅ Connected and initialized successfully!")

                # 2. Fetch the tools exposed by the server
                response = await session.list_tools()

                # 3. Format the tools into Ollama's expected JSON schema
                ollama_tools = []
                for tool in response.tools:
                    print(f"🛠️ Found tool: {tool.name}")
                    ollama_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.inputSchema,
                            },
                        }
                    )

                if not ollama_tools:
                    print("⚠️ No tools found on the MCP server.")
                    return

                print("\n💬 Start chatting! (type 'quit' to exit)")
                messages = []

                # 4. Interactive Chat Loop
                while True:
                    user_input = input("\nYou: ")
                    if user_input.lower() in ["quit", "exit"]:
                        break

                    messages.append({"role": "user", "content": user_input})
                    print(f"🤔 {MODEL_NAME} is thinking...")

                    response = ollama.chat(model=MODEL_NAME, messages=messages, tools=ollama_tools)

                    msg = response.get("message", {})
                    messages.append(msg)

                    # 5. Check if the model decided to execute a tool
                    if msg.get("tool_calls"):
                        for tool_call in msg["tool_calls"]:
                            func = tool_call["function"]
                            t_name = func["name"]
                            t_args = func["arguments"]

                            print(f"⚡ Model requested tool: {t_name} with args: {t_args}")

                            # Execute the requested tool
                            try:
                                result = await session.call_tool(t_name, arguments=t_args)
                                tool_text = "\n".join(
                                    [c.text for c in result.content if c.type == "text"]
                                )
                                print(f"✅ Tool returned: {tool_text}")

                                messages.append(
                                    {"role": "tool", "content": tool_text, "name": t_name}
                                )

                            except Exception as e:
                                print(f"❌ Tool execution failed: {e}")
                                messages.append(
                                    {"role": "tool", "content": f"Error: {str(e)}", "name": t_name}
                                )

                        # 6. Generate final response based on tool output
                        print("🧠 Generating final response based on tool output...")
                        final_response = ollama.chat(model=MODEL_NAME, messages=messages)

                        final_msg = final_response.get("message", {})
                        messages.append(final_msg)
                        print(f"\n🤖 Ollama: {final_msg.get('content')}")

                    else:
                        print(f"\n🤖 Ollama: {msg.get('content')}")

    except httpx.ConnectTimeout:
        print(f"\n❌ NETWORK ERROR: Timed out trying to connect to {MCP_SERVER_URL}.")
        print("Please check that your MCP server is running and port 8000 is accessible.")
    except httpx.ConnectError:
        print(f"\n❌ NETWORK ERROR: Connection refused at {MCP_SERVER_URL}.")
        print("The server is either not running, or blocking the connection.")
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(main())
