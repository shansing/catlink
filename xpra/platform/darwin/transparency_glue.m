/**
 * This file is part of Xpra.
 * Copyright (C) 2018 Antoine Martin <antoine@devloop.org.uk>
 */

#import <Cocoa/Cocoa.h>

static NSEvent *rememberedButtonPressEvent = nil;
static NSTimeInterval rememberedButtonPressTime = 0;

void clearRememberedButtonPressEvent(void) {
    [rememberedButtonPressEvent release];
    rememberedButtonPressEvent = nil;
    rememberedButtonPressTime = 0;
}

void rememberButtonPressEvent(void) {
    NSEvent *event = [NSApp currentEvent];
    if (event && [event type] == NSEventTypeLeftMouseDown) {
        clearRememberedButtonPressEvent();
        rememberedButtonPressEvent = [event retain];
        rememberedButtonPressTime = [NSDate timeIntervalSinceReferenceDate];
    }
}

BOOL performWindowDragWithRememberedEvent(NSWindow *window, double maxAge) {
    if (!window) {
        return NO;
    }
    if (!rememberedButtonPressEvent) {
        return NO;
    }
    if ([rememberedButtonPressEvent window] != window) {
        clearRememberedButtonPressEvent();
        return NO;
    }
    NSTimeInterval age = [NSDate timeIntervalSinceReferenceDate] - rememberedButtonPressTime;
    if (age < 0 || age > maxAge) {
        clearRememberedButtonPressEvent();
        return NO;
    }
    [window performWindowDragWithEvent:rememberedButtonPressEvent];
    clearRememberedButtonPressEvent();
    return YES;
}

void setOpaque(NSWindow *window, BOOL opaque) {
	[window setOpaque:opaque];
}
void setClearBackgroundColor(NSWindow *window) {
	NSColor *color = [NSColor clearColor];
	[window setBackgroundColor:color];
}
void setBackgroundColor(NSWindow *window, NSColor *color) {
	[window setBackgroundColor:color];
}

float getBackingScaleFactor(NSWindow *window) {
    NSScreen *screen = [window screen];
    if (!screen) {
        return 1;
    }
    return screen.backingScaleFactor;
}
