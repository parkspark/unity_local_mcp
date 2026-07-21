// Canonical data-driven level loader installed by the unity_local_mcp host
// (unity_install_level_loader). Do not hand-edit in the Unity project — the host
// overwrites this file with its template on every install.
//
// Reads Assets/StreamingAssets/Levels/<levelFile> at runtime and builds the level.
// Field names of the [Serializable] classes below MUST match the host's
// level_schema.py keys exactly (JsonUtility matches by field name).
//
// Deterministic console markers (used by the host for machine verification):
//   [LevelLoader] Loaded <name>: ...
//   [LevelLoader] GOAL reached: <name>
//   [LevelLoader] ALL LEVELS CLEAR
//   [LevelLoader] ERROR ...

using System;
using System.IO;
using UnityEngine;

public class LevelLoader : MonoBehaviour
{
    [Tooltip("File name inside Assets/StreamingAssets/Levels/, e.g. level1.json")]
    public string levelFile = "level1.json";

    string currentLevelName = "";
    string nextLevelFile = "";
    GameObject levelRoot;

    void Start()
    {
        Load(levelFile);
    }

    public void Load(string file)
    {
        string path = Path.Combine(Application.streamingAssetsPath, "Levels", file);
        if (!File.Exists(path))
        {
            Debug.LogError($"[LevelLoader] ERROR level file not found: {path}");
            return;
        }

        LevelData data;
        try
        {
            data = JsonUtility.FromJson<LevelData>(File.ReadAllText(path));
        }
        catch (Exception e)
        {
            Debug.LogError($"[LevelLoader] ERROR could not parse {file}: {e.Message}");
            return;
        }
        if (data == null || data.objects == null || data.objects.Length == 0)
        {
            Debug.LogError($"[LevelLoader] ERROR {file} has no objects");
            return;
        }

        if (levelRoot != null) Destroy(levelRoot);
        levelRoot = new GameObject("LevelRoot");

        levelFile = file;
        currentLevelName = string.IsNullOrEmpty(data.name) ? file : data.name;
        nextLevelFile = data.next_level ?? "";

        int platforms = 0, hazards = 0, decorations = 0;
        foreach (var obj in data.objects)
        {
            switch (obj.type)
            {
                case "platform": BuildCube(obj, new Color(0.5f, 0.5f, 0.55f), false); platforms++; break;
                case "hazard": BuildHazard(obj); hazards++; break;
                case "decoration": BuildCube(obj, new Color(0.4f, 0.6f, 0.9f), true); decorations++; break;
                default:
                    Debug.LogWarning($"[LevelLoader] unknown object type '{obj.type}' skipped");
                    break;
            }
        }

        BuildGoal(data.goal);
        PlacePlayer(ToVec3(data.player_spawn, Vector3.zero));

        Debug.Log($"[LevelLoader] Loaded {currentLevelName}: {platforms} platforms, " +
                  $"{hazards} hazards, {decorations} decorations, goal at {ToVec3(data.goal.position, Vector3.zero)}");
    }

    public void LoadNext()
    {
        if (string.IsNullOrEmpty(nextLevelFile))
        {
            Debug.Log("[LevelLoader] ALL LEVELS CLEAR");
            return;
        }
        Load(nextLevelFile);
    }

    public void RespawnPlayer()
    {
        // Reload positions from the current level's spawn without rebuilding.
        var player = GameObject.Find("Player");
        if (player != null) Debug.Log("[LevelLoader] Player hit hazard — respawning");
        Load(levelFile);
    }

    public void OnGoalReached()
    {
        Debug.Log($"[LevelLoader] GOAL reached: {currentLevelName}");
        LoadNext();
    }

    GameObject BuildCube(LevelObject obj, Color fallback, bool noCollider)
    {
        var go = GameObject.CreatePrimitive(PrimitiveType.Cube);
        go.name = string.IsNullOrEmpty(obj.name) ? obj.type : obj.name;
        go.transform.SetParent(levelRoot.transform, false);
        go.transform.localPosition = ToVec3(obj.position, Vector3.zero);
        go.transform.localScale = ToVec3(obj.size, Vector3.one);
        if (noCollider) Destroy(go.GetComponent<Collider>());
        Tint(go, obj.color, fallback);
        return go;
    }

    void BuildHazard(LevelObject obj)
    {
        var go = BuildCube(obj, new Color(0.9f, 0.15f, 0.15f), false);
        go.GetComponent<Collider>().isTrigger = true;
        go.AddComponent<LevelLoaderHazardZone>().loader = this;
    }

    void BuildGoal(GoalSpec goal)
    {
        var go = GameObject.CreatePrimitive(PrimitiveType.Cube);
        go.name = "Goal";
        go.transform.SetParent(levelRoot.transform, false);
        go.transform.localPosition = ToVec3(goal.position, Vector3.zero);
        go.transform.localScale = ToVec3(goal.size, Vector3.one);
        go.GetComponent<Collider>().isTrigger = true;
        Tint(go, null, new Color(0.2f, 0.85f, 0.3f));
        go.AddComponent<LevelLoaderGoalZone>().loader = this;
    }

    void PlacePlayer(Vector3 spawn)
    {
        var player = GameObject.Find("Player");
        if (player == null)
        {
            Debug.LogWarning("[LevelLoader] no GameObject named 'Player' found — spawn not applied");
            return;
        }
        player.transform.position = spawn;
        var rb = player.GetComponent<Rigidbody>();
        if (rb != null)
        {
            rb.linearVelocity = Vector3.zero;
            rb.angularVelocity = Vector3.zero;
        }
    }

    static void Tint(GameObject go, float[] color, Color fallback)
    {
        var renderer = go.GetComponent<Renderer>();
        if (renderer == null) return;
        Color c = fallback;
        if (color != null && color.Length >= 3)
            c = new Color(color[0], color[1], color[2], color.Length > 3 ? color[3] : 1f);
        renderer.material.color = c;
    }

    static Vector3 ToVec3(float[] v, Vector3 fallback)
    {
        return (v != null && v.Length >= 3) ? new Vector3(v[0], v[1], v[2]) : fallback;
    }

    // ------------------------------------------------------------ data classes
    // Field names mirror level_schema.py exactly (JsonUtility name matching).

    [Serializable]
    public class LevelData
    {
        public int version;
        public string name;
        public string next_level;
        public float[] player_spawn;
        public GoalSpec goal;
        public LevelObject[] objects;
    }

    [Serializable]
    public class GoalSpec
    {
        public float[] position;
        public float[] size;
    }

    [Serializable]
    public class LevelObject
    {
        public string type;
        public string name;
        public float[] position;
        public float[] size;
        public float[] color;
    }

    public class LevelLoaderGoalZone : MonoBehaviour
    {
        public LevelLoader loader;

        void OnTriggerEnter(Collider other)
        {
            if (other.name == "Player" || other.transform.root.name == "Player")
                loader.OnGoalReached();
        }
    }

    public class LevelLoaderHazardZone : MonoBehaviour
    {
        public LevelLoader loader;

        void OnTriggerEnter(Collider other)
        {
            if (other.name == "Player" || other.transform.root.name == "Player")
                loader.RespawnPlayer();
        }
    }
}
