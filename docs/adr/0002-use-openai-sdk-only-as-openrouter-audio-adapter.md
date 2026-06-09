# Use OpenAI SDK Only as OpenRouter Audio Adapter

OpenAI is removed as an API provider for YT Short Clipper. Gemini, accessed through the Google GenAI SDK, is the Gemini Text Provider for highlight finding and shared publishing metadata generation. OpenRouter is the OpenRouter Media Provider for caption transcription and hook voice generation.

The OpenAI Python SDK may remain only as an OpenRouter Audio Adapter pointed at OpenRouter's API base URL. This preserves compatibility with OpenAI-shaped audio calls, including transcription timestamp granularities when OpenRouter supports them, without reintroducing OpenAI API keys, OpenAI models, or selectable OpenAI provider settings.
