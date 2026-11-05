/* PosPro engine: command-line entry point.
 *
 *   pospro-engine extract | gnss | ins | combine | export | version
 *
 * Runs as a separate process so that a fault in processing can never take
 * QGIS down, and so forward and backward passes run on separate cores.
 */
#include <string.h>
#include <stdlib.h>
#include "pp.h"
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

int main(int argc, char **argv)
{
    const char *cmd;
    if (argc < 2) {
        fprintf(stderr, "PosPro engine %s\nusage: %s extract|gnss|ins|combine|export|version ...\n",
                PP_VERSION, argv[0]);
        return 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
#ifdef _WIN32
    /* RTKLIB splits Windows paths at '\\' only: normalise separators */
    {
        int i;
        char *q;
        for (i = 2; i < argc; i++) {
            const char *a = argv[i];
            int is_path = a[0] == '/' || a[0] == '.' ||
                          (((a[0] | 32) >= 'a' && (a[0] | 32) <= 'z') && a[1] == ':');
            if (!is_path) continue;
            for (q = argv[i]; *q; q++) if (*q == '/') *q = '\\';
        }
    }
#endif
    cmd = argv[1];
    if (!strcmp(cmd, "version")) {
        printf("RESULT version=%s\nRESULT rtklib=%s %s\n", PP_VERSION, VER_RTKLIB, PATCH_LEVEL);
        return 0;
    }
    if (!strcmp(cmd, "extract")) return cmd_extract(argc - 2, argv + 2);
    if (!strcmp(cmd, "gnss")) return cmd_gnss(argc - 2, argv + 2);
    if (!strcmp(cmd, "ins")) return cmd_ins(argc - 2, argv + 2);
    if (!strcmp(cmd, "combine")) return cmd_combine(argc - 2, argv + 2);
    if (!strcmp(cmd, "pos2trj")) return cmd_pos2trj(argc - 2, argv + 2);
    if (!strcmp(cmd, "export")) return cmd_export(argc - 2, argv + 2);
    if (!strcmp(cmd, "navcheck")) return cmd_navcheck(argc - 2, argv + 2);
    fprintf(stderr, "unknown command %s\n", cmd);
    return 2;
}
