import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { initials } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * A user's avatar, falling back to their initials.
 *
 * The fallback is not decorative: most accounts have no profile image, so the
 * initials circle is what the directory actually shows in nearly every row.
 * `alt` is empty because the user's name is always rendered beside the avatar —
 * announcing it twice would only add noise for a screen-reader user.
 */
export function UserAvatar({
  name,
  imageUrl,
  className,
}: {
  name: string;
  imageUrl?: string | null;
  className?: string;
}) {
  return (
    <Avatar className={cn("size-9", className)}>
      {imageUrl ? <AvatarImage src={imageUrl} alt="" /> : null}
      <AvatarFallback className="bg-muted text-xs font-medium text-muted-foreground">
        {initials(name)}
      </AvatarFallback>
    </Avatar>
  );
}
