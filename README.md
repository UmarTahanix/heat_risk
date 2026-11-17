heat_risk is a Python-based CLI made with Cursor agent and Open-Meteo MCP server.
It takes at least one argument (city name) "python heat_risk.py --city <city_name>" and outputs a clean, readable report, showing temperature, humidity and heat index (calculated from NWS formula from temperature_2m and relative_humidity_2m, tools from MCP), and 3-day high/low forecast.
The report is in .txt format as default, but there are other formats can be entered, like json and csv as another argument, like "python heat_risk.py --city <city_name> --output <format>"
