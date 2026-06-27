param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 6667
)
# ClaudeComms IRC server launcher.
#   .\run-server.ps1                      -> localhost only
#   .\run-server.ps1 -BindHost 0.0.0.0    -> reachable on the network
python "$PSScriptRoot/../server/ircd.py" --host $BindHost --port $Port
