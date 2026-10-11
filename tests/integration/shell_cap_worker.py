"""Helper: one ensure attempt in a separate OS process.
Used by the process-level cap regression: two workers race the same cap
and exactly one must win. Prints OK <name> or FAIL <code>, exits 0/2.
"""
import asyncio
import sys


async def main():
    image, key, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])
    from redteam.capability.shell_policy import ShellWorkroomPolicy
    from redteam.capability.shell_workroom import ShellWorkroomManager
    policy = ShellWorkroomPolicy(image=image, allow_unpinned_image=True, max_workrooms=cap)
    manager = ShellWorkroomManager(policy)
    try:
        name = await manager.ensure_workroom(key)
    except Exception as exc:
        print("FAIL " + type(exc).__name__ + " " + str(exc))
        return 2
    print("OK " + name)
    return 0
sys.exit(asyncio.run(main()))
