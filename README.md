# extract_npc_dialogs.py

Extracts NPC dialogs from your C# `NpcHandler` class and writes them as Lua string tables, so the text can live outside the source code (and be translated).

## What it does

1. Reads the `NpcID` class and builds a map of `Name = id` pairs.
2. Finds every handler method marked with `[NpcAttribute(NpcID.Something)]` in the `NpcHandler` class.
3. Inside each method, collects every `data.AddText(...)` and `data.AddOption(...)` call (comments are ignored).
4. Writes one Lua file per NPC, sorted by NPC id, plus one `common.lua` for strings that are repeated a lot.

## Requirements

- Python 3.8 or newer
- No extra packages

## Quick start

```
python extract_npc_dialogs.py --npc-id NpcID.cs --handler NpcHandler.cs --out-dir npc_strings
```

`--npc-id` and `--handler` accept one or more files, or folders (all `*.cs` files inside are read):

```
python extract_npc_dialogs.py --npc-id Enums/NpcID.cs --handler Npcs/ --out-dir npc_strings
```

## Example

C# input:

```csharp
[NpcAttribute(NpcID.HuntersGuild)]
public static void HuntersGuild(Client.GameClient client, ServerSockets.Packet stream, byte Option, string Input, uint id)
{
    data.AddText("Hello Hunter " + client.Player.Name + ", Welcome to [" + Program.ServerConfig.ServerName + " Server].\n");
    data.AddText("The rules are very easy hunt or be hunted\n");
    data.AddOption("Global Rank", 1);
    data.AddOption("Just passing by..", 255);
}
```

with `HuntersGuild = 5001,` in the NpcID class, produces `npcs/HuntersGuild-5001.lua`:

```lua
return {
    -- HuntersGuild [5001]
    -- {0}=client.Player.Name, {1}=Program.ServerConfig.ServerName
    ["STR_ID_HuntersGuild[5001][Text1]"] = "Hello Hunter {0}, Welcome to [{1} Server].\n",
    ["STR_ID_HuntersGuild[5001][Text2]"] = "The rules are very easy hunt or be hunted\n",
    ["STR_ID_HuntersGuild[5001][Option1]"] = "Global Rank",
    ["STR_ID_HuntersGuild[5001][Option2]"] = "@STR_COMMON[2]",
}
```

## Output layout

```
npc_strings/
  common.lua                          strings used many times
  npcs/
    HuntersGuild-5001.lua             one file per NPC, named <Name>-<Id>.lua
    DeadRabbitDungeonMainGate-32175.lua
    CpCastle-45478.lua
    ...
```

NPCs are processed in ascending id order. Every file is a valid Lua table (`return { ... }`).

## Key format

```
STR_ID_<NpcName>[<NpcId>][Text<n>]
STR_ID_<NpcName>[<NpcId>][Option<n>]
```

- `<NpcName>` is the name of the matching entry in the NpcID class.
- `<n>` counts in source order across the whole method, including all `if (Option == ...)` branches.
- If one NPC id has several handler methods, they are merged into one file and the numbering continues.
- If one method has several `[NpcAttribute]` lines, the first id is used and the other ids are listed in a comment at the top of the file.

## Dynamic text (placeholders)

Variables and expressions inside a string are replaced by `{0}`, `{1}`, ... and the original C# expressions are written in a comment above the entry. Both concatenation (`"a" + x + "b"`) and interpolation (`$"a {x} b"`) are supported. Your code has to fill the placeholders when it shows the text.

## Common strings

A string that appears `--min-dup` times or more (default 3) is stored once in `common.lua` under a numeric id:

```lua
-- common.lua
return {
    ["STR_COMMON[1]"] = "--------------------------------------------------------------------------------------------------------\n",
    ["STR_COMMON[2]"] = "Just passing by..",
}
```

NPC entries that use it store a reference instead of the text. A reference is the value of `--ref-prefix` (default `@`) followed by the common key:

```lua
["STR_ID_HuntersGuild[5001][Option2]"] = "@STR_COMMON[2]",
```

Common ids are numbered in order of first appearance (NPCs sorted by id), starting at `--common-start`.

### Which strings count as the same

By default, strings are treated as the same when they are equal after:

- ignoring upper/lower case and extra spaces
- treating `~` as a space
- ignoring all punctuation
- comparing only letters, digits and `{n}` placeholders

So `Thanks`, `Thanks.` and `thanks.` become one entry, and so do `Yes please!` and `Yes,~please.`. The text stored in `common.lua` is the most frequent spelling of the group.

Things that are never merged:

- a text and an option with the same words (they are counted separately)
- strings that differ in the number of trailing `\n`
- strings made only of symbols (`-----`, `...`) unless they are written identically

Use `--strict-dup` to merge only identical strings. Use `--report merges.txt` to get a list of which spellings were merged into each common id, which is the quickest way to check the result.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--npc-id PATH...` | required | File(s) or folder(s) with the NpcID class |
| `--handler PATH...` | required | File(s) or folder(s) with the NpcHandler class |
| `--out-dir DIR` | `npc_strings` | Output folder (`common.lua` and `npcs/` are created inside) |
| `--attr NAME` | `NpcAttribute` | Name of the attribute used on handler methods |
| `--min-dup N` | `3` | Move a string to `common.lua` when it appears at least N times |
| `--strict-dup` | off | Merge only identical strings |
| `--report FILE` | none | Write a list of merged spellings per common id |
| `--common-start N` | `1` | First common id |
| `--ref-prefix STR` | `@` | Prefix that marks a value as a reference to a common key |
| `--option-by-id` | off | Name options by the id in `AddOption` (`Option255`) instead of `Option1..n` |
| `--strip-newlines` | off | Remove the trailing `\n` from texts |
| `--no-table` | off | Write bare entries without the `return { ... }` wrapper |

## Using the files in your code

A minimal Lua loader showing the two things the game code has to do, resolve `@` references and fill `{n}` placeholders:

```lua
local common = dofile("npc_strings/common.lua")
local npc    = dofile("npc_strings/npcs/HuntersGuild-5001.lua")

local function get(key, ...)
    local value = npc[key]
    if value and value:sub(1, 1) == "@" then
        value = common[value:sub(2)]
    end
    local args = { ... }
    return (value:gsub("{(%d+)}", function(i) return tostring(args[tonumber(i) + 1]) end))
end

print(get("STR_ID_HuntersGuild[5001][Text1]", "Alex", "MyServer"))
```

## Translating

Translated files should keep the same keys and structure. Only the values change. Keep these as they are:

- `~` (used for spaces in options and at the start of continued lines)
- `{0}`, `{1}`, ... placeholders
- `@STR_COMMON[...]` references (translate the text in `common.lua` instead)

## Limitations

- Only string expressions directly inside `AddText(...)` and `AddOption(...)` are read. Dialogs built through helper methods, `string.Format` or other calls are not detected.
- A part of a string that is not a literal (a variable, a method call) always becomes a placeholder.
- Handlers whose enum name is not found in the NpcID class are skipped with a warning.
- Existing files are overwritten, but files of NPCs that no longer exist are not deleted. Clear the `npcs/` folder before a fresh run if you want it clean.
- Common ids can change when NPCs are added or removed, because they are numbered by first appearance. If your code stores these ids anywhere, regenerate everything together.
