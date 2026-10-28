namespace WorkBuddyUninstall;

internal static class DpiUtil
{
    public static float Scale
    {
        get
        {
            try
            {
                using var graphics = Graphics.FromHwnd(IntPtr.Zero);
                return graphics.DpiX / 96f;
            }
            catch
            {
                return 1f;
            }
        }
    }
}
